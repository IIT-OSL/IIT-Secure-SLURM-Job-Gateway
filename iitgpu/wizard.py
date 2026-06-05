# iitgpu/wizard.py
from __future__ import annotations

import getpass
import grp
import os
import re
import shutil
from datetime import datetime, timezone, timedelta


def _cluster_tz():
    try:
        from iitgpu.config import cluster_tz
        return cluster_tz()
    except Exception:
        return timezone(timedelta(hours=5, minutes=30))
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import load_config, jobs_dir, user_dir
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
    "train":       "Train from scratch",
    "finetune":    "Fine-tune a model",
    "inference":   "Run inference / generate output",
    "test":        "Quick test  (30 min, reduced resources)",
    "notebook":    "Notebook (JupyterLab)  — interactive GPU session",
    "notebook-script": "Run a notebook (.ipynb) as a batch job  — executes it end-to-end",
    "interactive": "Interactive shell on the GPU node  (srun --pty)",
}


def _browse_script(start_dir: str, jail=in_jail, exts=(".py", ".sh")) -> str | None:
    """Jailed file browser that only shows files with one of *exts* (plus dirs).

    `jail` is the navigation predicate: the global `in_jail` for admins, or the
    caller's per-user browse jail for regular users so they stay confined to
    their own shared/users/<user> area (plus shared read-only models/envs).
    `exts` selects which files are pickable (default scripts; (".ipynb",) for
    the notebook-as-batch-job flow).
    """
    current = start_dir
    while True:
        entries = safe_listdir(current)
        dirs = sorted(e for e in entries if Path(current, e).is_dir())
        files = sorted(
            e for e in entries
            if Path(current, e).is_file() and e.endswith(tuple(exts))
        )
        choices = ["[.. up]"] + [f"[dir] {d}" for d in dirs] + files + ["[cancel]"]
        choice = questionary.select(
            f"Browse ({current}):", choices=choices, style=_STYLE
        ).ask()
        if choice is None or choice == "[cancel]":
            return None
        if choice == "[.. up]":
            parent = str(Path(current).parent)
            if jail(parent):
                current = parent
            else:
                warn("Already at root of allowed paths.")
            continue
        if choice.startswith("[dir] "):
            candidate = str(Path(current) / choice[6:])
            if jail(candidate):
                current = candidate
            else:
                warn("Access denied.")
            continue
        chosen = str(Path(current) / choice)
        if jail(chosen):
            return chosen
        warn("Access denied.")
        return None


# A conservative pip package-spec matcher (name, optional extras, optional
# version pins). Anything with shell metacharacters or spaces is rejected so a
# package list can never inject into the generated job script.
_PKG_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?"
    r"([=<>!~]=?[A-Za-z0-9._*+-]+)*$"
)


def _valid_pkg_tokens(raw: str) -> list[str]:
    """Keep only tokens that look like safe pip package specs."""
    return [t for t in raw.split() if _PKG_RE.match(t)]


def _notebook_deps_prompt(notebook_path: str, browse_jail, start_dir: str) -> tuple[str, str]:
    """Ask how to install Python deps before a notebook runs / a session starts.
    When *notebook_path* is given, auto-detects a requirements.txt next to it or in
    its project root. Returns (requirements_path, packages_str) — at most one set."""
    found = ""
    if notebook_path:
        nb = Path(notebook_path)
        candidates = [nb.parent / "requirements.txt", nb.parent.parent / "requirements.txt"]
        found = next((str(c) for c in candidates
                      if c.is_file() and browse_jail(str(c))), "")
    auto = f"Install from {found}  (auto-detected)" if found else None
    choices = ([auto] if auto else []) + [
        "Choose a requirements.txt file",
        "Type package names (e.g. tqdm wfdb h5py)",
        "Skip — my environment already has everything",
    ]
    sel = questionary.select(
        "Install Python dependencies first?",
        choices=choices, style=_STYLE,
    ).ask()
    if not sel or sel.startswith("Skip"):
        return "", ""
    if auto and sel == auto:
        return found, ""
    if sel.startswith("Choose"):
        picked = _browse_script(start_dir, browse_jail, exts=(".txt",))
        return (picked or ""), ""
    if sel.startswith("Type"):
        raw = questionary.text("Packages (space-separated):", style=_STYLE).ask() or ""
        toks = _valid_pkg_tokens(raw)
        if raw.strip() and not toks:
            warn("No valid package names recognised — skipping dependency install.")
        return "", " ".join(toks)
    return "", ""


def _browse_data_folder(start_dir: str, jail=in_jail) -> str | None:
    """Jailed folder browser (directories only, for picking a data directory).

    `jail` is the navigation predicate (see `_browse_script`): regular users are
    confined to their own area; admins get the full global jail.
    """
    current = start_dir
    while True:
        entries = safe_listdir(current)
        dirs = sorted(e for e in entries if Path(current, e).is_dir())
        choices = (["[.. up]"] + [f"[dir] {d}" for d in dirs]
                   + ["[select this folder]", "[cancel]"])
        choice = questionary.select(
            f"Browse ({current}):", choices=choices, style=_STYLE
        ).ask()
        if choice is None or choice == "[cancel]":
            return None
        if choice == "[select this folder]":
            if jail(current):
                return current
            warn("Access denied.")
            return None
        if choice == "[.. up]":
            parent = str(Path(current).parent)
            if jail(parent):
                current = parent
            else:
                warn("Already at root of allowed paths.")
            continue
        if choice.startswith("[dir] "):
            candidate = str(Path(current) / choice[6:])
            if jail(candidate):
                current = candidate
            else:
                warn("Access denied.")
            continue


def _ensure_scripts_dir(cfg, user: str) -> str | None:
    """Create /shared/users/<user>/scripts/ with 0o770 + gpuusers gid, return path."""
    scripts_dir = Path(user_dir(cfg, user)) / "scripts"
    dest = str(scripts_dir)
    if not in_jail(dest):
        warn("Scripts directory is outside the allowed jail.")
        return None
    scripts_dir.mkdir(parents=True, exist_ok=True)
    scripts_dir.chmod(0o770)
    try:
        gid = grp.getgrnam(cfg.gpuusers_group).gr_gid
        os.chown(dest, -1, gid)
    except (KeyError, PermissionError, OSError):
        pass
    return dest


def _inline_paste(cfg, user: str) -> tuple[str | None, str | None]:
    """Collect pasted data, write to /shared/users/<user>/data/<ts>_inline.txt.

    Returns (data_path, script_path_or_None).
    script_path is non-None only if user agrees to use generated script as job.
    """
    info("Paste your data below. When finished, enter a line containing only EOF and press Enter.")
    lines = []
    try:
        while True:
            line = input()
            if line == "EOF":
                break
            lines.append(line)
    except EOFError:
        pass

    if not lines:
        warn("No data pasted.")
        return None, None

    ts = datetime.now(_cluster_tz()).strftime("%Y%m%d_%H%M%S")
    data_subdir = Path(user_dir(cfg, user)) / "data"
    data_subdir.mkdir(parents=True, exist_ok=True)
    data_dest = str(data_subdir / f"{ts}_inline.txt")

    if not in_jail(data_dest):
        err("Data destination is outside the allowed jail — refused.")
        return None, None

    content = "\n".join(lines) + "\n"
    Path(data_dest).write_text(content)
    Path(data_dest).chmod(0o644)
    auditclient.log("data_inline_paste", detail=Path(data_dest).name,
                    meta={"path": data_dest, "bytes": len(content.encode())})
    ok(f"Saved {len(lines)} lines to {data_dest}")

    script_path: str | None = None
    if questionary.confirm(
        "Create a Python script to load this data?", default=True, style=_STYLE
    ).ask():
        scripts_dir = _ensure_scripts_dir(cfg, user)
        if scripts_dir:
            script_dest = str(Path(scripts_dir) / f"{ts}_load_data.py")
            if not in_jail(script_dest):
                warn("Script destination is outside the allowed jail — skipping.")
            else:
                loader = (
                    "#!/usr/bin/env python3\n"
                    "# Auto-generated data loader — edit as needed\n"
                    "# Data file: " + data_dest + "\n"
                    "\n"
                    "import os\n"
                    "\n"
                    "DATA_PATH = os.environ.get(\"DATA_PATH\", \"" + data_dest + "\")\n"
                    "\n"
                    "\n"
                    "def load_data():\n"
                    "    with open(DATA_PATH, \"r\") as f:\n"
                    "        content = f.read()\n"
                    "    lines = content.splitlines()\n"
                    "    print(f\"Loaded {len(lines)} lines from {DATA_PATH}\")\n"
                    "    print(\"First 5 lines:\")\n"
                    "    for line in lines[:5]:\n"
                    "        print(f\"  {line}\")\n"
                    "    return content\n"
                    "\n"
                    "\n"
                    "if __name__ == \"__main__\":\n"
                    "    data = load_data()\n"
                )
                Path(script_dest).write_text(loader)
                Path(script_dest).chmod(0o644)
                auditclient.log("script_generated", detail=script_dest)
                ok(f"Loader script saved to {script_dest}")
                if questionary.confirm(
                    "Use this generated script as your job script?",
                    default=True, style=_STYLE,
                ).ask():
                    script_path = script_dest

    return data_dest, script_path


def _validate_and_show_errors(script_text: str, username: str, cfg) -> bool:
    """Run validate_sbatch; print errors and return False if any found."""
    from iitgpu.validate import validate_sbatch
    from iitgpu.ui import err as _err
    errors = validate_sbatch(script_text, username, cfg)
    if errors:
        for e in errors:
            _err(f"  Script error: {e}")
        return False
    return True


def _tier2_edit(script_text: str, username: str, cfg) -> str | None:
    """Open the generated script in an editor; validate on save. Returns final text or None."""
    import subprocess, tempfile, difflib
    from iitgpu.ui import info, warn, err as _err
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
    while True:
        with tempfile.NamedTemporaryFile(suffix=".sbatch", mode="w",
                                         delete=False, prefix="iitgpu_") as tf:
            tf.write(script_text)
            tmpfile = tf.name
        try:
            subprocess.run([editor, tmpfile])
            with open(tmpfile) as f:
                edited = f.read()
        except (OSError, FileNotFoundError) as exc:
            _err(f"Editor failed ({exc}). Falling back to nano.")
            try:
                subprocess.run(["nano", tmpfile])
                with open(tmpfile) as f:
                    edited = f.read()
            except OSError:
                return None
        finally:
            try:
                os.unlink(tmpfile)
            except OSError:
                pass

        if not _validate_and_show_errors(edited, username, cfg):
            import questionary
            if not questionary.confirm("Fix errors and try again?", default=True,
                                       style=_STYLE).ask():
                return None
            script_text = edited
            continue

        diff = list(difflib.unified_diff(
            script_text.splitlines(keepends=True),
            edited.splitlines(keepends=True),
            fromfile="generated", tofile="edited",
        ))
        if diff:
            from iitgpu import auditclient
            auditclient.log("sbatch_edited", meta={"diff": "".join(diff[:40])})
        return edited


def _tier3_own_script(username: str, cfg) -> str | None:
    """Browse to a user's .sbatch file; validate and return its content."""
    import questionary
    from iitgpu.validate import in_user_browse_jail, user_browse_roots, in_jail
    from iitgpu.ui import info, err as _err
    from iitgpu import auditclient

    info("Browse to your .sbatch file (must be inside your allowed directories).")
    roots = user_browse_roots(cfg.nfs_root, username)
    start = roots[0] if roots else cfg.nfs_root
    current = start

    while True:
        try:
            entries = [e for e in os.listdir(current)
                       if os.path.isdir(os.path.join(current, e))
                       or e.endswith(".sbatch")]
        except OSError:
            _err("Cannot list directory.")
            return None
        choices = ["[.. up]"] + \
                  [f"[dir] {e}" for e in sorted(entries) if os.path.isdir(os.path.join(current, e))] + \
                  [e for e in sorted(entries) if e.endswith(".sbatch")] + \
                  ["[cancel]"]
        pick = questionary.select(f"Browse ({current}):", choices=choices,
                                  style=_STYLE).ask()
        if pick is None or pick == "[cancel]":
            return None
        if pick == "[.. up]":
            parent = str(Path(current).parent)
            if in_jail(parent):
                current = parent
            continue
        if pick.startswith("[dir] "):
            candidate = str(Path(current) / pick[6:])
            if in_jail(candidate):
                current = candidate
            continue
        # selected a .sbatch file
        sbatch_path = str(Path(current) / pick)
        if not in_jail(sbatch_path):
            _err("File is outside the allowed directory.")
            return None
        try:
            text = Path(sbatch_path).read_text()
        except OSError as exc:
            _err(f"Cannot read file: {exc}")
            return None
        if not _validate_and_show_errors(text, username, cfg):
            import questionary
            if not questionary.confirm("Select a different file?", default=True,
                                       style=_STYLE).ask():
                return None
            continue
        auditclient.log("sbatch_own_script", meta={"path": sbatch_path})
        return text


def run_wizard(prefill: dict | None = None) -> None:  # noqa: C901 (complexity ok for a wizard)
    cfg = load_config()
    jdir = jobs_dir(cfg)
    user = getpass.getuser()

    # Role-aware file-browser jail (mirrors files.py). Regular users browse and
    # pick data/scripts from their own shared/users/<user> area (plus read-only
    # shared models/envs); admins get the full NFS jail. Used as the navigation
    # predicate for _browse_data_folder / _browse_script below.
    from iitgpu.config import is_admin
    from iitgpu.validate import in_user_browse_jail
    _admin = is_admin(cfg)
    _browse_jail = (
        in_jail if _admin
        else (lambda p: in_user_browse_jail(p, cfg.nfs_root, user))
    )

    def _user_browse_start() -> str:
        """The user's own folder, created if missing, so the browser always opens
        inside shared/users/<user> instead of falling back to the NFS root."""
        if _admin:
            return cfg.nfs_root
        start = user_dir(cfg, user)
        try:
            Path(start).mkdir(parents=True, exist_ok=True)
        except OSError:
            return start if _browse_jail(start) else cfg.nfs_root
        return start

    header("New Job")

    # ── Step 0: Optional template load (skip when prefilling from rerun) ─────
    _tdefaults: dict = prefill or {}
    if not _tdefaults:
        if questionary.confirm(
            "Load from a saved template?", default=False, style=_STYLE
        ).ask():
            from iitgpu.templates import pick_template
            tdata = pick_template(cfg)
            if tdata:
                _tdefaults = tdata

    _prefill_hint = "  [pre-filled from previous run]" if prefill else ""

    # ── Step 1: Task type ─────────────────────────────────────────────────────
    _template_task_type = _tdefaults.get("task_type", "")
    _default_label = _TASK_LABELS.get(_template_task_type, list(_TASK_LABELS.values())[0])

    task_choice = questionary.select(
        "Step 1 — What are you doing?" + (_prefill_hint if _template_task_type else ""),
        choices=list(_TASK_LABELS.values()),
        default=_default_label,
        style=_STYLE,
    ).ask()
    if task_choice is None:
        return
    task_type = next(k for k, v in _TASK_LABELS.items() if v == task_choice)
    defaults = resource_defaults(task_type)

    # ── Interactive GPU session (srun --pty) early return ────────────────────
    if task_type == "interactive":
        from iitgpu.jobs import build_interactive_cmd
        spec = JobSpec(
            job_name="interactive", partition=cfg.partition,
            gpus=defaults.gpus, cpus=defaults.cpus, mem_gb=defaults.mem_gb,
            time_limit=defaults.time_limit or "02:00:00", run_command="",
            task_type="interactive",
        )
        cmd = build_interactive_cmd(spec, partition=cfg.partition)
        info("Requesting an interactive GPU allocation — you will land in a shell")
        info("ON the compute node. It ends when you type 'exit' or the time limit hits.")
        panel("Interactive command", " ".join(cmd))
        if not questionary.confirm(
            "Start interactive session now?", default=True, style=_STYLE
        ).ask():
            return
        if not auditclient.log_or_block("interactive_start", detail="srun_pty"):
            err("Audit logging failed. Refusing to start (safety policy).")
            return
        import subprocess
        try:
            subprocess.run(cmd)
        except (OSError, KeyboardInterrupt):
            pass
        auditclient.log("interactive_end")
        info("Interactive session ended.")
        return

    # ── Step 2: Environment ───────────────────────────────────────────────────
    from iitgpu.envs import list_all_envs
    from iitgpu.containers import list_images, validate_image
    envs = list_all_envs(cfg)
    chosen_env = None
    chosen_container: str = ""

    _prefill_conda = _tdefaults.get("conda_env", "")
    _prefill_container = _tdefaults.get("container_image", "")
    if _prefill_conda:
        _env_type_default = "Conda / venv environment"
    elif _prefill_container:
        _env_type_default = "Container image  (.sif via Apptainer)"
    else:
        _env_type_default = "Conda / venv environment"

    env_type = questionary.select(
        "Step 2 — Environment type:"
        + (_prefill_hint if (_prefill_conda or _prefill_container) else ""),
        choices=[
            "Conda / venv environment",
            "Container image  (.sif via Apptainer)",
            "[none / skip]",
        ],
        default=_env_type_default,
        style=_STYLE,
    ).ask()
    if env_type is None:
        return

    if env_type == "Conda / venv environment":
        if not envs:
            warn("No environments registered. Run Settings → Build environment first.")
            if not questionary.confirm(
                "Continue without an environment?", default=False, style=_STYLE
            ).ask():
                return
        else:
            env_choices = [f"{e.name}  ({e.kind})" for e in envs] + ["[none / skip]"]
            _env_sel_default = None
            if _prefill_conda:
                _env_sel_default = next(
                    (f"{e.name}  ({e.kind})"
                     for e in envs
                     if e.path == _prefill_conda or e.name == _prefill_conda),
                    None,
                )
            env_sel = questionary.select(
                "Which environment?"
                + (_prefill_hint if _env_sel_default else ""),
                choices=env_choices,
                default=_env_sel_default,
                style=_STYLE,
            ).ask()
            if env_sel is None:
                return
            if env_sel != "[none / skip]":
                chosen_name = env_sel.split("  (")[0]
                chosen_env = next((e for e in envs if e.name == chosen_name), None)

    elif env_type == "Container image  (.sif via Apptainer)":
        images = list_images(cfg.nfs_root)
        if not images:
            warn(f"No .sif images found in {cfg.nfs_root}/images/")
            warn("Build or pull images first (see deploy/build-images.md).")
            if not questionary.confirm(
                "Enter image path manually?", default=False, style=_STYLE
            ).ask():
                return
            manual = questionary.text("Full path to .sif image:", style=_STYLE).ask()
            if not manual or not manual.strip():
                return
            chosen_container = manual.strip()
        else:
            img_choices = (
                [Path(i).name + "  " + i for i in images]
                + ["[enter path manually]", "[cancel]"]
            )
            _img_default = None
            if _prefill_container:
                _img_default = next(
                    (Path(i).name + "  " + i for i in images if i == _prefill_container),
                    None,
                )
            img_sel = questionary.select(
                "Which container image?"
                + (_prefill_hint if _img_default else ""),
                choices=img_choices,
                default=_img_default,
                style=_STYLE,
            ).ask()
            if img_sel is None or img_sel == "[cancel]":
                return
            if img_sel == "[enter path manually]":
                manual = questionary.text("Full path to .sif image:", style=_STYLE).ask()
                if not manual or not manual.strip():
                    return
                chosen_container = manual.strip()
            else:
                chosen_container = img_sel.split("  ", 1)[1].strip()

        if chosen_container and not validate_image(chosen_container):
            warn("Image path is outside the allowed jail or not a .sif — rejected.")
            return
        auditclient.log("container_selected", detail=Path(chosen_container).name)

    # ── Step 3: Your data (skip for notebook) ────────────────────────────────
    data_path: str = ""
    script_path: str | None = None
    job_name = task_type

    if task_type != "notebook":
        if task_type == "notebook-script":
            info("Upload your notebook (.ipynb) AND its data/files now, using the")
            info("SAME relative paths the notebook expects (e.g. ./data/...). You'll")
            info("pick which .ipynb to run in the next step.")
        _prefill_dp = _tdefaults.get("data_path", "")
        data_choices = [
            "a) Pick an existing folder",
            "b) Upload from my computer  (scp/rsync instructions)",
            "c) Download from a URL",
            "d) Paste data inline",
            "e) Skip (no data needed)",
        ]
        _data_default = "a) Pick an existing folder" if _prefill_dp else None

        data_choice = questionary.select(
            "Step 3 — Your data:"
            + (_prefill_hint if _prefill_dp else ""),
            choices=data_choices,
            default=_data_default,
            style=_STYLE,
        ).ask()
        if data_choice is None:
            return

        if data_choice.startswith("a)"):
            _start = _user_browse_start()
            if _prefill_dp and Path(_prefill_dp).exists() and _browse_jail(_prefill_dp):
                _start = (
                    _prefill_dp if Path(_prefill_dp).is_dir()
                    else str(Path(_prefill_dp).parent)
                )
            _picked = _browse_data_folder(_start, _browse_jail)
            if _picked:
                data_path = _picked

        elif data_choice.startswith("b)"):
            from iitgpu.upload import run_upload
            run_upload()

        elif data_choice.startswith("c)"):
            # _download_from_url(folder_path) writes files into a folder and returns None.
            # Build a per-user download staging folder, then pass it as the target.
            _dl_folder = str(Path(user_dir(cfg, user)) / "data" / "downloads")
            if in_jail(_dl_folder):
                Path(_dl_folder).mkdir(parents=True, exist_ok=True)
                from iitgpu.upload import _download_from_url
                _download_from_url(_dl_folder)
                data_path = _dl_folder
            else:
                from iitgpu.upload import run_upload
                run_upload()

        elif data_choice.startswith("d)"):
            _dp, _sp = _inline_paste(cfg, user)
            if _dp:
                data_path = _dp
            if _sp:
                script_path = _sp  # skip Step 5 browser

    # ── Step 4: Your model (finetune / inference only) ────────────────────────
    model_path: str = _tdefaults.get("model_path", "")

    if task_type in ("finetune", "inference"):
        model_choices = [
            "a) Pick a downloaded model from registry",
            "b) Download from HuggingFace now",
            "c) Enter a path manually",
            "d) Skip",
        ]
        model_choice = questionary.select(
            "Step 4 — Your model:", choices=model_choices, style=_STYLE
        ).ask()
        if model_choice is None:
            return

        if model_choice.startswith("a)"):
            try:
                from iitgpu.models import pick_model
                _picked_model = pick_model(cfg)
                if _picked_model is not None:
                    model_path = _picked_model.path
            except (ImportError, AttributeError):
                from iitgpu.models import model_menu
                model_menu(cfg)

        elif model_choice.startswith("b)"):
            # download_hf(cfg, repo_id) requires a repo_id; prompt first.
            _repo_id = questionary.text(
                "HuggingFace repo ID (e.g. mistralai/Mistral-7B-v0.1):",
                style=_STYLE,
            ).ask()
            if _repo_id and _repo_id.strip():
                try:
                    from iitgpu.models import download_hf
                    _ok, _dest = download_hf(cfg, _repo_id.strip())
                    if _ok and _dest:
                        model_path = _dest
                except (ImportError, AttributeError):
                    from iitgpu.models import model_menu
                    model_menu(cfg)

        elif model_choice.startswith("c)"):
            _manual_mp = questionary.text("Full model path:", style=_STYLE).ask()
            if _manual_mp and _manual_mp.strip():
                if in_jail(_manual_mp.strip()):
                    model_path = _manual_mp.strip()
                else:
                    warn("Path is outside the allowed jail — rejected.")

    # ── Step 5: Notebook config OR Script ─────────────────────────────────────
    if task_type == "notebook":
        port_str = questionary.text(
            "Step 5 — JupyterLab port (on the GPU node):", default="8888", style=_STYLE
        ).ask()
        if port_str is None:
            return
        try:
            nb_port = max(1024, min(65535, int(port_str.strip())))
        except ValueError:
            nb_port = 8888

        # Optionally pre-install deps so the interactive session starts ready
        # (no notebook selected here, so no auto-detect — choose a file or type).
        info("Tip: install your project's deps now so cells don't fail on import.")
        nb_requirements, nb_packages = _notebook_deps_prompt("", _browse_jail, _user_browse_start())

        spec = JobSpec(
            job_name=job_name,
            partition="gpu",
            gpus=defaults.gpus,
            cpus=defaults.cpus,
            mem_gb=defaults.mem_gb,
            time_limit=defaults.time_limit,
            run_command="",
            task_type=task_type,
            conda_env=chosen_env.path if chosen_env and chosen_env.kind == "conda" else "",
            venv_path=chosen_env.path if chosen_env and chosen_env.kind == "venv" else "",
            container_image=chosen_container,
        )
        # Auto-populate SLURM mail directive from users.db if an MTA is available.
        from iitgpu.notify import mta_present
        from iitgpu import daemonclient
        if mta_present():
            _registered_email = daemonclient.email_for(user)
            if _registered_email:
                spec.mail_user = _registered_email
        folder = make_job_folder(jdir, spec)
        from iitgpu.jobs import render_notebook_sbatch
        script_text = render_notebook_sbatch(
            spec, folder, port=nb_port,
            gateway_host=cfg.gateway_host, gateway_port=int(cfg.gateway_port),
            requirements=nb_requirements, packages=nb_packages,
        )
        panel("Generated notebook sbatch script", script_text)

        action = questionary.select(
            "What would you like to do?",
            choices=["Submit notebook job", "Discard"],
            style=_STYLE,
        ).ask()
        if action is None or action == "Discard":
            shutil.rmtree(folder, ignore_errors=True)
            info("Discarded.")
            return

        sbatch_path = str(Path(folder) / "job.sbatch")
        Path(sbatch_path).write_text(script_text)
        Path(sbatch_path).chmod(0o644)
        kv("Script saved", sbatch_path)

        if not auditclient.log_or_block("notebook_submit", detail=job_name):
            err("Audit logging failed. Refusing to submit (safety policy).")
            return

        success, result = submit_job(sbatch_path)
        if success:
            ok(f"Notebook job submitted! ID: {result}")
            ok(
                f"SSH tunnel: ssh -p {cfg.gateway_port} "
                f"-L {nb_port}:localhost:{nb_port} {user}@{cfg.gateway_host}"
            )
            auditclient.log("notebook_submitted_ok", detail=job_name, job_id=result)
        else:
            err(f"Submission failed: {result}")
            auditclient.log("notebook_submit_failed", detail=result)
        return

    # Non-notebook: show script browser if not already set from inline paste
    if script_path is None:
        _start = _user_browse_start()
        _prefill_sp = _tdefaults.get("script_path", "")
        if _prefill_sp and Path(_prefill_sp).exists() and _browse_jail(_prefill_sp):
            _start = str(Path(_prefill_sp).parent)

        if task_type == "notebook-script":
            info("Step 5 — Select the notebook (.ipynb) to run end-to-end:")
            info("Make sure you uploaded it and its data above, with the paths the")
            info("notebook expects. Results (executed.ipynb + .html) land in the job folder.")
            script_path = _browse_script(_start, _browse_jail, exts=(".ipynb",))
        else:
            info("Step 5 — Select your job script (.py or .sh):")
            script_path = _browse_script(_start, _browse_jail)
        if script_path is None:
            return

    # ── Step 5a: Notebook dependencies (install before running the .ipynb) ────
    nb_requirements = nb_packages = ""
    nb_auto_install = True
    if task_type == "notebook-script" and script_path:
        nb_requirements, nb_packages = _notebook_deps_prompt(
            script_path, _browse_jail, _user_browse_start())
        nb_auto_install = questionary.confirm(
            "Auto-install any other missing imports during the run? "
            "(recommended — catches deps like tensorboard automatically)",
            default=True, style=_STYLE,
        ).ask()
        if nb_auto_install is None:
            return

    # ── Step 5b: Training config (train_cifar10.py special-case) ─────────────
    training_flags = ""
    if script_path and Path(script_path).name == "train_cifar10.py":
        model_sel = questionary.select(
            "Model:",
            choices=[
                "SmallResNet    — fast    (~2 min / 50 epochs, 0.6 GB VRAM, ~93-95% acc)",
                "WideResNet-28-10 — accurate (~14 min / 50 epochs, 26 GB VRAM, ~95-96% acc)",
            ],
            style=_STYLE,
        ).ask()
        if model_sel is None:
            return
        if "WideResNet" in model_sel:
            training_flags += " --model wideres"

        epochs_str = questionary.text("Epochs:", default="50", style=_STYLE).ask()
        if epochs_str is None:
            return
        try:
            ep = max(1, int(epochs_str.strip()))
            if ep != 50:
                training_flags += f" --epochs {ep}"
        except ValueError:
            pass

    # ── Step 6: Arguments ─────────────────────────────────────────────────────
    from iitgpu.validate import clean_array_spec, clean_dependency
    args = ""
    # nbconvert takes no user args (notebooks aren't parameterised here), so skip
    # the prompt for the notebook-as-batch-job flow.
    if task_type != "notebook-script":
        _prefill_args = _tdefaults.get("extra_args", "")
        raw_args = questionary.text(
            "Step 6 — Extra arguments (blank = none):"
            + (_prefill_hint if _prefill_args else ""),
            default=_prefill_args,
            style=_STYLE,
        ).ask()
        if raw_args is None:
            return
        args = clean_run_command(raw_args) if raw_args.strip() else ""

    # ── Job array (optional) ──────────────────────────────────────────────────
    array_spec = ""
    _prefill_array = _tdefaults.get("array", "")
    if questionary.confirm(
        "Run as a job array (parameter sweep)?",
        default=bool(_prefill_array), style=_STYLE,
    ).ask():
        raw = questionary.text(
            "Array spec (e.g. 0-9 or 1-100%4):",
            default=_prefill_array,
            style=_STYLE,
        ).ask()
        cleaned = clean_array_spec(raw or "")
        if cleaned:
            array_spec = cleaned
            info("Array tasks expose $SLURM_ARRAY_TASK_ID; use it to index your sweep.")
        elif raw:
            warn("Invalid array spec — ignoring.")

    # ── Dependency (optional) ─────────────────────────────────────────────────
    dependency = ""
    _prefill_dep = _tdefaults.get("dependency", "")
    if questionary.confirm(
        "Wait for another job to finish first?",
        default=bool(_prefill_dep), style=_STYLE,
    ).ask():
        from iitgpu.slurm import queue as _q
        myjobs = _q()
        if myjobs:
            choices = [f"{e.job_id}  {e.name}  [{e.state}]" for e in myjobs] + ["[enter ID manually]"]
            sel = questionary.select(
                "Run after which job (on success)?", choices=choices, style=_STYLE
            ).ask()
            parent = (
                sel.split()[0] if sel and sel != "[enter ID manually]"
                else (questionary.text("Parent job ID:", style=_STYLE).ask() or "")
            )
        else:
            parent = questionary.text("Parent job ID:", style=_STYLE).ask() or ""
        dep = (
            clean_dependency(f"afterok:{parent.strip()}")
            if parent.strip().isdigit() else None
        )
        if dep:
            dependency = dep
        elif parent:
            warn("Invalid parent job ID — ignoring dependency.")

    # ── Build job spec ────────────────────────────────────────────────────────
    if task_type == "notebook-script" and script_path:
        from iitgpu.jobs import notebook_run_command
        run_cmd = notebook_run_command(
            script_path, in_container=bool(chosen_container),
            requirements=nb_requirements, packages=nb_packages,
            auto_install=nb_auto_install)
    elif script_path and script_path.endswith(".py"):
        run_cmd = f"python {script_path}"
    elif script_path:
        run_cmd = f"bash {script_path}"
    else:
        run_cmd = ""
    if training_flags:
        run_cmd += training_flags
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
        container_image=chosen_container,
        array=array_spec,
        dependency=dependency,
        model_path=model_path,
        data_path=data_path,
    )

    # Auto-populate SLURM mail directive from users.db if an MTA is available.
    from iitgpu.notify import mta_present
    from iitgpu import daemonclient
    if mta_present():
        _registered_email = daemonclient.email_for(user)
        if _registered_email:
            spec.mail_user = _registered_email

    folder = make_job_folder(jdir, spec)
    script_text = render_sbatch(spec, folder)

    # ── Preview summary ───────────────────────────────────────────────────────
    _env_display = "none"
    if chosen_env:
        _env_display = f"{chosen_env.name}  ({chosen_env.kind})"
    elif chosen_container:
        _env_display = f"container: {Path(chosen_container).name}"

    summary_lines = (
        f"  Data path  : {data_path or 'not set'}\n"
        f"  Model path : {model_path or 'not set'}\n"
        f"  Environment: {_env_display}\n"
        f"  Script     : {script_path or '(none)'}"
    )
    panel("Job Summary", summary_lines)
    panel("Generated sbatch script", script_text)

    # ── Action ────────────────────────────────────────────────────────────────
    action = questionary.select(
        "What would you like to do?",
        choices=[
            "Submit job",
            "Review & edit script, then submit",
            "Bring your own .sbatch, then submit",
            "Save as template + submit",
            "Save template only",
            "Discard",
        ],
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

    # ── Tier 2: Review & edit script ─────────────────────────────────────────
    if action == "Review & edit script, then submit":
        script_text = _tier2_edit(script_text, user, cfg)
        if script_text is None:
            shutil.rmtree(folder, ignore_errors=True)
            info("Discarded.")
            return

    # ── Tier 3: Bring your own .sbatch ────────────────────────────────────────
    if action == "Bring your own .sbatch, then submit":
        script_text = _tier3_own_script(user, cfg)
        if script_text is None:
            shutil.rmtree(folder, ignore_errors=True)
            info("Discarded.")
            return

    # ── Submit ────────────────────────────────────────────────────────────────
    sbatch_path = str(Path(folder) / "job.sbatch")
    Path(sbatch_path).write_text(script_text)
    Path(sbatch_path).chmod(0o644)
    kv("Script saved", sbatch_path)

    _submit_meta: dict = {"run_command": spec.run_command, "task_type": spec.task_type}
    if spec.conda_env:
        _submit_meta["conda_env"] = spec.conda_env
    if spec.venv_path:
        _submit_meta["venv_path"] = spec.venv_path
    if spec.container_image:
        _submit_meta["container_image"] = spec.container_image
    if spec.model_path:
        _submit_meta["model_path"] = spec.model_path
    if spec.data_path:
        _submit_meta["data_path"] = spec.data_path
    if spec.array:
        _submit_meta["array"] = spec.array
    if spec.dependency:
        _submit_meta["dependency"] = spec.dependency
    if not auditclient.log_or_block("job_submit", detail=job_name, meta=_submit_meta):
        err("Audit logging failed. Refusing to submit (safety policy).")
        return

    success, result = submit_job(sbatch_path)
    if success:
        ok(f"Job submitted! ID: {result}")
        auditclient.log("job_submitted_ok", detail=job_name, job_id=result)
        if spec.mail_user:
            info(f"SLURM will email [cyan]{spec.mail_user}[/] when the job ends.")
        if questionary.confirm(
            "Wait here for the result?", default=False, style=_STYLE
        ).ask():
            from iitgpu.notify import poll_until_done
            info("Waiting for the job to finish (Ctrl-C to stop waiting)…")
            try:
                final = poll_until_done(result, interval=10)
                ok(f"Job {result} finished: {final}")
            except KeyboardInterrupt:
                info("Stopped waiting (job keeps running).")
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
