from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import load_config, jobs_dir
from iitgpu.jobs import JobSpec, make_job_folder, save_upload, write_sbatch, render_sbatch
from iitgpu.slurm import get_partitions, submit_job
from iitgpu.ui import err, header, info, kv, ok, panel, warn
from iitgpu.validate import (
    MAX_CPUS, MAX_GPUS, MAX_HOURS, MAX_MEM_GB,
    clamp_int, clean_job_name, clean_modules, clean_run_command,
    clean_time_limit, in_jail, safe_listdir,
)

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
    ("selected", "fg:magenta"),
])


def _browse_file(start_dir: str) -> str | None:
    current = start_dir
    while True:
        entries = safe_listdir(current)
        dirs = sorted(e for e in entries if Path(current, e).is_dir())
        files = sorted(e for e in entries if Path(current, e).is_file())
        choices = ["[.. up]"] + [f"[dir] {d}" for d in dirs] + files + ["[cancel]"]
        choice = questionary.select(f"Browse: {current}", choices=choices, style=_STYLE).ask()
        if choice is None or choice == "[cancel]":
            return None
        if choice == "[.. up]":
            parent = str(Path(current).parent)
            if in_jail(parent):
                current = parent
            else:
                warn("Cannot navigate outside allowed paths.")
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


def _manual_edit(script_text: str) -> str:
    """Open $EDITOR if available; otherwise show inline and return as-is."""
    editor = os.environ.get("EDITOR", "")
    if editor:
        with tempfile.NamedTemporaryFile(
            suffix=".sbatch", mode="w", delete=False, prefix="iitgpu_"
        ) as tmp:
            tmp.write(script_text)
            tmp_path = tmp.name
        try:
            subprocess.run([editor, tmp_path])
            auditclient.log("script_manual_edit", detail=f"editor={editor}")
            return Path(tmp_path).read_text()
        except OSError as exc:
            warn(f"Could not open editor: {exc}")
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    else:
        # No editor — show the script and let user confirm as-is.
        info("[dim]Tip: set $EDITOR to enable in-place editing.[/]")
        panel("Current script (read-only preview)", script_text)
        auditclient.log("script_manual_edit", detail="inline_preview_only")
    return script_text


def _apply_template_defaults(tdata: dict) -> dict:
    """Fill in any keys the template might be missing so the wizard has safe defaults."""
    defaults = {
        "job_name": "", "partition": "", "gpus": 1, "cpus": 4,
        "mem_gb": 16, "time_limit": "01:00:00", "run_command": "",
        "modules": [], "uploads": [], "model_path": "", "conda_env": "", "venv_path": "",
    }
    defaults.update(tdata)
    return defaults


def run_wizard() -> None:
    cfg = load_config()
    jdir = jobs_dir(cfg)
    header("Create & Submit GPU Job")

    # ── Template quick-start ──────────────────────────────────────────────────
    tdefaults: dict = {}
    if questionary.confirm("Load from a template or preset?", default=False, style=_STYLE).ask():
        from iitgpu.templates import pick_template
        tdata = pick_template(cfg)
        if tdata:
            tdefaults = _apply_template_defaults(tdata)
            ok(f"Loaded template: {tdefaults.get('job_name', '')}")

    # ── Job name ─────────────────────────────────────────────────────────────
    raw_name = questionary.text(
        "Job name:", default=tdefaults.get("job_name", ""), style=_STYLE
    ).ask()
    if raw_name is None:
        return
    job_name = clean_job_name(raw_name)
    if not job_name:
        err("Invalid job name.")
        return

    # ── Partition ─────────────────────────────────────────────────────────────
    partitions = get_partitions()
    part_choices = [p.name for p in partitions] if partitions else ["gpu-short", "gpu-long"]
    default_part = tdefaults.get("partition", part_choices[0])
    if default_part not in part_choices:
        default_part = part_choices[0]
    partition = questionary.select(
        "Partition:", choices=part_choices, default=default_part, style=_STYLE
    ).ask()
    if partition is None:
        return

    # ── Resources ─────────────────────────────────────────────────────────────
    raw_gpus = questionary.text(
        f"GPUs (1-{MAX_GPUS}):", default=str(tdefaults.get("gpus", 1)), style=_STYLE
    ).ask()
    if raw_gpus is None:
        return
    gpus = clamp_int(raw_gpus, 1, MAX_GPUS, 1)

    raw_cpus = questionary.text(
        f"CPUs (1-{MAX_CPUS}):", default=str(tdefaults.get("cpus", 4)), style=_STYLE
    ).ask()
    if raw_cpus is None:
        return
    cpus = clamp_int(raw_cpus, 1, MAX_CPUS, 4)

    raw_mem = questionary.text(
        f"Memory GB (1-{MAX_MEM_GB}):", default=str(tdefaults.get("mem_gb", 16)), style=_STYLE
    ).ask()
    if raw_mem is None:
        return
    mem_gb = clamp_int(raw_mem, 1, MAX_MEM_GB, 16)

    while True:
        raw_time = questionary.text(
            f"Time limit HH:MM:SS (max {MAX_HOURS}h):",
            default=tdefaults.get("time_limit", "01:00:00"),
            style=_STYLE,
        ).ask()
        if raw_time is None:
            return
        time_limit = clean_time_limit(raw_time)
        if time_limit:
            break
        err("Invalid time format. Use HH:MM:SS.")

    # ── Model picker ──────────────────────────────────────────────────────────
    model_path = tdefaults.get("model_path", "")
    if questionary.confirm("Use a model from the library?", default=bool(model_path), style=_STYLE).ask():
        from iitgpu.models import pick_model
        entry = pick_model(cfg)
        if entry:
            model_path = entry.path
            kv("Model path", model_path)
            auditclient.log("model_selected", detail=entry.name)

    # ── Environment picker ────────────────────────────────────────────────────
    conda_env = tdefaults.get("conda_env", "")
    venv_path = tdefaults.get("venv_path", "")
    if questionary.confirm("Use a specific conda/venv environment?", default=bool(conda_env or venv_path), style=_STYLE).ask():
        from iitgpu.envs import pick_env
        env = pick_env(cfg)
        if env:
            if env.kind == "conda":
                conda_env = env.name
                venv_path = ""
            else:
                venv_path = env.path
                conda_env = ""
            kv("Environment", f"{env.kind}: {env.name}")
            auditclient.log("env_selected", detail=f"{env.kind}:{env.name}")

    # ── File uploads ──────────────────────────────────────────────────────────
    uploads: list[str] = list(tdefaults.get("uploads", []))
    while questionary.confirm("Attach a file?", default=False, style=_STYLE).ask():
        chosen = _browse_file(str(Path.home()))
        if chosen:
            uploads.append(chosen)
            ok(f"Added: {chosen}")

    # ── Run command ───────────────────────────────────────────────────────────
    raw_cmd = questionary.text(
        "Run command:", default=tdefaults.get("run_command", ""), style=_STYLE
    ).ask()
    if raw_cmd is None:
        return
    run_command = clean_run_command(raw_cmd)
    if not run_command.strip():
        err("Run command cannot be empty.")
        return

    # ── Modules ───────────────────────────────────────────────────────────────
    default_mods = " ".join(tdefaults.get("modules", []))
    raw_mods = questionary.text(
        "Modules to load (space-separated, blank=none):", default=default_mods, style=_STYLE
    ).ask()
    if raw_mods is None:
        return
    modules = clean_modules(raw_mods) if raw_mods.strip() else []

    # ── Build spec & preview ──────────────────────────────────────────────────
    spec = JobSpec(
        job_name=job_name, partition=partition, gpus=gpus, cpus=cpus,
        mem_gb=mem_gb, time_limit=time_limit, run_command=run_command,
        modules=modules, uploads=uploads,
        model_path=model_path, conda_env=conda_env, venv_path=venv_path,
    )
    folder = make_job_folder(jdir, spec)
    script_text = render_sbatch(spec, folder)
    panel("Generated sbatch script", script_text)

    # ── Manual edit ───────────────────────────────────────────────────────────
    if questionary.confirm("Edit the script manually before submitting?", default=False, style=_STYLE).ask():
        script_text = _manual_edit(script_text)

    # ── Action ────────────────────────────────────────────────────────────────
    action = questionary.select(
        "What would you like to do?",
        choices=["Submit job", "Save as template + submit", "Save template only", "Discard"],
        style=_STYLE,
    ).ask()

    if action is None or action == "Discard":
        import shutil as _sh
        _sh.rmtree(folder, ignore_errors=True)
        info("Discarded.")
        return

    # Save as template if requested
    if action in ("Save as template + submit", "Save template only"):
        tname = questionary.text(
            "Template name:", default=job_name, style=_STYLE
        ).ask()
        if tname and tname.strip():
            from iitgpu.templates import save_template
            if save_template(cfg, tname.strip(), spec):
                ok(f"Template '{tname.strip()}' saved.")

    if action == "Save template only":
        ok("Template saved. Not submitted.")
        auditclient.log("job_template_saved", detail=job_name)
        return

    for src in uploads:
        if in_jail(src):
            save_upload(src, folder)
        else:
            warn(f"Skipped non-jailed upload: {src}")

    # Write the (possibly manually edited) script to disk
    sbatch_path = str(Path(folder) / "job.sbatch")
    Path(sbatch_path).write_text(script_text)
    Path(sbatch_path).chmod(0o644)
    kv("Script saved", sbatch_path)

    # CRITICAL: audit-log BEFORE submitting; refuse if logging fails
    if not auditclient.log_or_block("job_submit", detail=job_name):
        err("Audit logging failed. Refusing to submit (safety policy).")
        return

    success, result = submit_job(sbatch_path)
    if success:
        ok(f"Job submitted! ID: {result}")
        auditclient.log("job_submitted_ok", detail=job_name, job_id=result)
    else:
        err(f"Submission failed: {result}")
        auditclient.log("job_submit_failed", detail=result)
