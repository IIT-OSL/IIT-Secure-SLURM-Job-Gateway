# iitgpu/setup.py
from __future__ import annotations

import getpass
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import Config, conda_sh, jobs_dir, load_config
from iitgpu import slurm as _slurm
from iitgpu.slurm import submit_job
from iitgpu.ui import err, header, info, kv, ok, warn
from iitgpu.validate import in_jail, safe_listdir

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
])


# ── Health check ──────────────────────────────────────────────────────────────

def check_cluster_health(cfg: Config) -> tuple[bool, list[str]]:
    """Returns (all_ok, list_of_error_messages)."""
    errors: list[str] = []

    partitions = _slurm.get_partitions()
    if not partitions:
        errors.append("sinfo returned no partitions — cluster may be unreachable")

    if not os.access(cfg.nfs_root, os.W_OK):
        errors.append(f"{cfg.nfs_root} is not writable — check NFS mount and permissions")

    return len(errors) == 0, errors


def _run_health_check(cfg: Config) -> bool:
    header("Cluster Health Check")
    ok_flag, errors = check_cluster_health(cfg)
    if ok_flag:
        ok("Cluster is reachable and shared storage is writable.")
        return True
    for msg in errors:
        err(msg)
    warn("Fix the above issues before continuing.")
    return False


# ── Environment setup ─────────────────────────────────────────────────────────

def _run_env_setup(cfg: Config) -> None:
    header("Environment Setup")
    from iitgpu.envbuilder import FRAMEWORK_LABELS, build_env

    framework_choices = list(FRAMEWORK_LABELS.values()) + ["[skip]"]
    choice = questionary.select(
        "Pick a base framework:", choices=framework_choices, style=_STYLE
    ).ask()
    if choice is None or choice == "[skip]":
        return

    framework_key = next(k for k, v in FRAMEWORK_LABELS.items() if v == choice)

    env_name = questionary.text(
        "Name for this environment:", default=framework_key, style=_STYLE
    ).ask()
    if not env_name or not env_name.strip():
        return
    env_name = env_name.strip().replace(" ", "-")

    req_path: str | None = None
    if questionary.confirm(
        "Add a requirements.txt on top? (optional)", default=False, style=_STYLE
    ).ask():
        req_path = _browse_file(cfg.nfs_root)
        if req_path and not req_path.endswith(".txt"):
            warn("Selected file doesn't look like a requirements.txt — using anyway.")

    auditclient.log("env_build_start", detail=f"{framework_key}:{env_name}")
    success, env_path = build_env(env_name, framework_key, req_path, cfg)
    if success:
        # Register as kind="conda" — envbuilder always uses `conda create -p`,
        # so sbatch activation must go through conda (not plain venv source).
        from iitgpu.envs import EnvEntry, _save_venv_registry, _load_venv_registry
        existing = _load_venv_registry(cfg)
        existing = [e for e in existing if e.name != env_name]
        existing.append(EnvEntry(name=env_name, kind="conda", path=env_path))
        _save_venv_registry(cfg, existing)
        ok(f"Environment '{env_name}' registered.")
        auditclient.log("env_build_ok", detail=env_name)
    else:
        auditclient.log("env_build_failed", detail=env_name)


# ── Data upload ───────────────────────────────────────────────────────────────

def _run_data_upload(cfg: Config) -> None:
    header("Data Upload")
    user = getpass.getuser()
    dest_dir = Path(cfg.nfs_root) / user / "data"
    dest_dir.mkdir(parents=True, exist_ok=True)

    info(f"Files will be copied to: {dest_dir}")
    while True:
        src = _browse_file(cfg.nfs_root)
        if src is None:
            break
        src_path = Path(src)
        dest = dest_dir / src_path.name
        try:
            if src_path.is_dir():
                shutil.copytree(str(src_path), str(dest), dirs_exist_ok=True)
            else:
                shutil.copy2(src, str(dest))
            ok(f"Copied → {dest}")
            auditclient.log("data_upload", detail=src_path.name)
        except OSError as exc:
            err(f"Copy failed: {exc}")

        if not questionary.confirm("Upload another file?", default=False, style=_STYLE).ask():
            break


# ── Smoke test ────────────────────────────────────────────────────────────────

def _build_smoke_test_script(env_path: str, cfg: Config, out_dir: str) -> str:
    out_path = str(Path(out_dir) / "slurm-%j.out")
    conda_sh_path = conda_sh(cfg)
    lines = [
        "#!/bin/bash",
        "#SBATCH --job-name=smoke_test",
        "#SBATCH --partition=gpu",
        "#SBATCH --gres=gpu:1",
        "#SBATCH --cpus-per-task=4",
        "#SBATCH --mem=16G",
        "#SBATCH --time=00:05:00",
        f"#SBATCH --chdir={cfg.nfs_root}",
        f"#SBATCH --output={out_path}",
        "",
        f"[ -f '{conda_sh_path}' ] && source '{conda_sh_path}'",
        f"conda activate {env_path}",
        "",
        "python3 - <<'PYEOF'",
        "import torch",
        "print('CUDA available:', torch.cuda.is_available())",
        "if torch.cuda.is_available():",
        "    print('GPU:', torch.cuda.get_device_name(0))",
        "PYEOF",
    ]
    return "\n".join(lines) + "\n"


def _run_smoke_test(cfg: Config) -> None:
    header("Smoke Test")
    from iitgpu.envs import list_all_envs
    envs = list_all_envs(cfg)
    if not envs:
        warn("No environments registered. Set one up in Environment Setup first.")
        return

    env_choices = [f"{e.name}  ({e.path})" for e in envs] + ["[skip]"]
    choice = questionary.select(
        "Which environment to test?", choices=env_choices, style=_STYLE
    ).ask()
    if choice is None or choice == "[skip]":
        return

    chosen_name = choice.split("  (")[0]
    env = next(e for e in envs if e.name == chosen_name)

    user = getpass.getuser()
    out_dir = str(Path(jobs_dir(cfg)) / user / "smoke_test")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    os.chmod(out_dir, 0o777)

    script = _build_smoke_test_script(env.path, cfg, out_dir)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".sh", dir=cfg.nfs_root, delete=False, prefix="iitgpu_smoke_"
    ) as tmp:
        tmp.write(script)
        tmp_path = tmp.name
    # NamedTemporaryFile defaults to 0600 (owner-only). sbatch runs as daham
    # (a different user) and can't open the script without world-read permission.
    os.chmod(tmp_path, 0o644)

    user_dir = Path(cfg.nfs_root) / getpass.getuser()
    user_dir.mkdir(parents=True, exist_ok=True)

    auditclient.log("smoke_test_start", detail=env.name)
    if not auditclient.log_or_block("job_submit", detail="smoke_test"):
        err("Audit logging failed. Refusing to submit (safety policy).")
        return
    success, result = submit_job(tmp_path)
    if success:
        ok(f"Smoke test submitted. Job ID: {result}")
        if questionary.confirm("Watch live output now?", default=True, style=_STYLE).ask():
            try:
                from iitgpu.dashboard import run_dashboard
                run_dashboard(job_id=result)
            except ImportError:
                info("Live dashboard not available. Check job output manually.")
    else:
        err(f"Smoke test submission failed: {result}")


# ── File browser (internal) ───────────────────────────────────────────────────

def _browse_file(start_dir: str) -> str | None:
    current = start_dir
    while True:
        entries = safe_listdir(current)
        dirs = sorted(e for e in entries if Path(current, e).is_dir())
        files = sorted(e for e in entries if Path(current, e).is_file())
        choices = ["[.. up]"] + [f"[dir] {d}" for d in dirs] + files + ["[cancel]"]
        choice = questionary.select(
            f"Browse ({current}):", choices=choices, style=_STYLE
        ).ask()
        if choice is None or choice == "[cancel]":
            return None
        if choice == "[.. up]":
            parent = str(Path(current).parent)
            if in_jail(parent):
                current = parent
            else:
                warn("Already at root of allowed paths.")
            continue
        if choice.startswith("[dir] "):
            candidate = str(Path(current) / choice[6:])
            if in_jail(candidate):
                current = candidate
            else:
                warn("Access denied.")
            continue
        chosen = str(Path(current) / choice)
        if in_jail(chosen):
            return chosen
        warn("Access denied.")
        return None


# ── Model download ────────────────────────────────────────────────────────────

def _run_model_download(cfg: Config) -> None:
    header("Model Download")
    from iitgpu.models import model_menu
    model_menu(cfg)


# ── Main setup menu ───────────────────────────────────────────────────────────

def run_setup() -> None:
    cfg = load_config()
    header("Setup")

    if not _run_health_check(cfg):
        return

    steps = [
        ("Environment (conda/venv)", _run_env_setup),
        ("Data upload",              _run_data_upload),
        ("Model download",           _run_model_download),
        ("Smoke test",               _run_smoke_test),
    ]

    for label, fn in steps:
        if questionary.confirm(f"Run: {label}?", default=True, style=_STYLE).ask():
            fn(cfg)
