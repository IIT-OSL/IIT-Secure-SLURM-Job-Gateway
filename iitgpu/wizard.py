# iitgpu/wizard.py
from __future__ import annotations

import getpass
import shutil
from datetime import datetime
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import load_config, jobs_dir
from iitgpu.jobs import JobSpec, make_job_folder, render_sbatch, resource_defaults
from iitgpu.slurm import submit_job
from iitgpu.ui import err, header, info, kv, ok, panel, warn
from iitgpu.validate import clean_run_command, in_jail, safe_listdir

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("selected", "fg:magenta"),
])

_TASK_LABELS: dict[str, str] = {
    "train":     "Train from scratch",
    "finetune":  "Fine-tune a model",
    "inference": "Run inference / generate output",
    "test":      "Quick test  (30 min, reduced resources)",
}


def _browse_script(start_dir: str) -> str | None:
    """Jailed file browser that only shows .py and .sh files (plus dirs)."""
    current = start_dir
    while True:
        entries = safe_listdir(current)
        dirs = sorted(e for e in entries if Path(current, e).is_dir())
        files = sorted(
            e for e in entries
            if Path(current, e).is_file() and e.endswith((".py", ".sh"))
        )
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


def run_wizard() -> None:
    cfg = load_config()
    jdir = jobs_dir(cfg)
    header("Run a Job")

    # ── Step 0: Optional template load ───────────────────────────────────────
    _tdefaults: dict = {}
    if questionary.confirm("Load from a saved template?", default=False, style=_STYLE).ask():
        from iitgpu.templates import pick_template
        tdata = pick_template(cfg)
        if tdata:
            _tdefaults = tdata

    # ── Step 1: Task type ─────────────────────────────────────────────────────
    # Pre-select task type from template if loaded
    _template_task_type = _tdefaults.get("task_type", "")
    _default_label = _TASK_LABELS.get(_template_task_type, list(_TASK_LABELS.values())[0])

    task_choice = questionary.select(
        "What are you doing?",
        choices=list(_TASK_LABELS.values()),
        default=_default_label,
        style=_STYLE,
    ).ask()
    if task_choice is None:
        return
    task_type = next(k for k, v in _TASK_LABELS.items() if v == task_choice)
    defaults = resource_defaults(task_type)

    # ── Step 2: Environment ───────────────────────────────────────────────────
    from iitgpu.envs import list_all_envs
    envs = list_all_envs(cfg)
    chosen_env = None
    if not envs:
        warn("No environments registered. Run Setup → Environment first.")
        if not questionary.confirm(
            "Continue without an environment?", default=False, style=_STYLE
        ).ask():
            return
    else:
        env_choices = [f"{e.name}  ({e.kind})" for e in envs] + ["[none / skip]"]
        env_sel = questionary.select(
            "Which environment?", choices=env_choices, style=_STYLE
        ).ask()
        if env_sel is None:
            return
        if env_sel != "[none / skip]":
            chosen_name = env_sel.split("  (")[0]
            chosen_env = next((e for e in envs if e.name == chosen_name), None)

    # ── Step 3: Script ────────────────────────────────────────────────────────
    start = str(Path(cfg.nfs_root) / getpass.getuser())
    if not Path(start).exists():
        start = cfg.nfs_root
    script_path = _browse_script(start)
    if script_path is None:
        return

    # ── Step 4: Arguments ─────────────────────────────────────────────────────
    raw_args = questionary.text(
        "Extra arguments (blank = none):", style=_STYLE
    ).ask()
    if raw_args is None:
        return
    args = clean_run_command(raw_args) if raw_args.strip() else ""

    # ── Build job spec ────────────────────────────────────────────────────────
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    job_name = f"{task_type}_{timestamp}"

    if script_path.endswith(".py"):
        run_cmd = f"python {script_path}"
    else:
        run_cmd = f"bash {script_path}"
    if args:
        run_cmd += f" {args}"

    spec = JobSpec(
        job_name=job_name,
        partition="gpu",
        gpus=defaults.gpus,
        cpus=defaults.cpus,
        mem_gb=defaults.mem_gb,
        time_limit=defaults.time_limit,
        run_command=run_cmd,
        task_type=task_type,
        conda_env=chosen_env.path if chosen_env and chosen_env.kind == "conda" else "",
        venv_path=chosen_env.path if chosen_env and chosen_env.kind == "venv" else "",
    )

    folder = make_job_folder(jdir, spec)
    script_text = render_sbatch(spec, folder)
    panel("Generated sbatch script", script_text)

    # ── Action ────────────────────────────────────────────────────────────────
    action = questionary.select(
        "What would you like to do?",
        choices=["Submit job", "Save as template + submit", "Save template only", "Discard"],
        style=_STYLE,
    ).ask()

    if action is None or action == "Discard":
        shutil.rmtree(folder, ignore_errors=True)
        info("Discarded.")
        return

    if action in ("Save as template + submit", "Save template only"):
        tname = questionary.text(
            "Template name:", default=job_name, style=_STYLE
        ).ask()
        if tname and tname.strip():
            from iitgpu.templates import save_template
            if save_template(cfg, tname.strip(), spec):
                ok(f"Template '{tname.strip()}' saved.")

    if action == "Save template only":
        auditclient.log("job_template_saved", detail=job_name)
        return

    # ── Submit ────────────────────────────────────────────────────────────────
    sbatch_path = str(Path(folder) / "job.sbatch")
    Path(sbatch_path).write_text(script_text)
    Path(sbatch_path).chmod(0o644)
    kv("Script saved", sbatch_path)

    if not auditclient.log_or_block("job_submit", detail=job_name):
        err("Audit logging failed. Refusing to submit (safety policy).")
        return

    success, result = submit_job(sbatch_path)
    if success:
        ok(f"Job submitted! ID: {result}")
        auditclient.log("job_submitted_ok", detail=job_name, job_id=result)
        if questionary.confirm(
            "Watch live output now?", default=True, style=_STYLE
        ).ask():
            try:
                from iitgpu.dashboard import run_dashboard
                run_dashboard(job_id=result)
            except ImportError:
                info("Live dashboard not available. Check job output manually.")
    else:
        err(f"Submission failed: {result}")
        auditclient.log("job_submit_failed", detail=result)
