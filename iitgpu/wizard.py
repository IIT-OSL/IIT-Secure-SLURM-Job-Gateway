# iitgpu/wizard.py
from __future__ import annotations

import getpass
import shutil
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
    "notebook":  "Notebook (JupyterLab)  — interactive GPU session",
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

    # ── Step 2: Environment (conda env OR container image) ──────────────────
    from iitgpu.envs import list_all_envs
    from iitgpu.containers import list_images, validate_image
    envs = list_all_envs(cfg)
    chosen_env = None
    chosen_container: str = ""

    env_type = questionary.select(
        "Environment type:",
        choices=[
            "Conda / venv environment",
            "Container image  (.sif via Apptainer)",
            "[none / skip]",
        ],
        style=_STYLE,
    ).ask()
    if env_type is None:
        return

    if env_type == "Conda / venv environment":
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
            img_choices = [Path(i).name + "  " + i for i in images] + ["[enter path manually]", "[cancel]"]
            img_sel = questionary.select(
                "Which container image?", choices=img_choices, style=_STYLE
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
            warn("Image path is outside the allowed jail or is not a .sif file — rejected.")
            return
        auditclient.log("container_selected", detail=Path(chosen_container).name)

    # ── Step 3: Script / Notebook config ─────────────────────────────────────
    job_name = task_type

    if task_type == "notebook":
        # Notebook jobs launch JupyterLab — no script path needed
        port_str = questionary.text(
            "JupyterLab port (on the GPU node):", default="8888", style=_STYLE
        ).ask()
        if port_str is None:
            return
        try:
            nb_port = max(1024, min(65535, int(port_str.strip())))
        except ValueError:
            nb_port = 8888

        spec = JobSpec(
            job_name=job_name,
            partition="gpu",
            gpus=defaults.gpus,
            cpus=defaults.cpus,
            mem_gb=defaults.mem_gb,
            time_limit=defaults.time_limit,
            run_command="",   # not used for notebooks
            task_type=task_type,
            conda_env=chosen_env.path if chosen_env and chosen_env.kind == "conda" else "",
            venv_path=chosen_env.path if chosen_env and chosen_env.kind == "venv" else "",
            container_image=chosen_container,
        )
        folder = make_job_folder(jdir, spec)
        from iitgpu.jobs import render_notebook_sbatch, write_notebook_sbatch
        script_text = render_notebook_sbatch(spec, folder, port=nb_port)
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
            ok(f"SSH tunnel: ssh -p 2225 -L {nb_port}:localhost:{nb_port} public@10.35.4.100")
            auditclient.log("notebook_submitted_ok", detail=job_name, job_id=result)
        else:
            err(f"Submission failed: {result}")
            auditclient.log("notebook_submit_failed", detail=result)
        return

    # ── Step 3 (non-notebook): Script ─────────────────────────────────────────
    start = str(Path(cfg.nfs_root) / getpass.getuser())
    if not Path(start).exists():
        start = cfg.nfs_root
    script_path = _browse_script(start)
    if script_path is None:
        return

    # ── Step 3.5: Training configuration (train_cifar10.py) ──────────────────
    training_flags = ""
    if Path(script_path).name == "train_cifar10.py":
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

        epochs_str = questionary.text(
            "Epochs:", default="50", style=_STYLE
        ).ask()
        if epochs_str is None:
            return
        try:
            ep = max(1, int(epochs_str.strip()))
            if ep != 50:
                training_flags += f" --epochs {ep}"
        except ValueError:
            pass

    # ── Step 4: Arguments ─────────────────────────────────────────────────────
    raw_args = questionary.text(
        "Extra arguments (blank = none):", style=_STYLE
    ).ask()
    if raw_args is None:
        return
    args = clean_run_command(raw_args) if raw_args.strip() else ""

    # ── Build job spec ────────────────────────────────────────────────────────

    if script_path.endswith(".py"):
        run_cmd = f"python {script_path}"
    else:
        run_cmd = f"bash {script_path}"
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
