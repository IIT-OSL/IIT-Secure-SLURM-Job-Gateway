# iitgpu/wizard.py
from __future__ import annotations
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


def run_wizard() -> None:
    cfg = load_config()
    jdir = jobs_dir(cfg)
    header("Create & Submit GPU Job")

    raw_name = questionary.text("Job name:", style=_STYLE).ask()
    if raw_name is None:
        return
    job_name = clean_job_name(raw_name)
    if not job_name:
        err("Invalid job name.")
        return

    partitions = get_partitions()
    part_choices = [p.name for p in partitions] if partitions else ["gpu-short", "gpu-long"]
    partition = questionary.select("Partition:", choices=part_choices, style=_STYLE).ask()
    if partition is None:
        return

    raw_gpus = questionary.text(f"GPUs (1-{MAX_GPUS}):", default="1", style=_STYLE).ask()
    if raw_gpus is None:
        return
    gpus = clamp_int(raw_gpus, 1, MAX_GPUS, 1)

    raw_cpus = questionary.text(f"CPUs (1-{MAX_CPUS}):", default="4", style=_STYLE).ask()
    if raw_cpus is None:
        return
    cpus = clamp_int(raw_cpus, 1, MAX_CPUS, 4)

    raw_mem = questionary.text(f"Memory GB (1-{MAX_MEM_GB}):", default="16", style=_STYLE).ask()
    if raw_mem is None:
        return
    mem_gb = clamp_int(raw_mem, 1, MAX_MEM_GB, 16)

    while True:
        raw_time = questionary.text(
            f"Time limit HH:MM:SS (max {MAX_HOURS}h):", default="01:00:00", style=_STYLE
        ).ask()
        if raw_time is None:
            return
        time_limit = clean_time_limit(raw_time)
        if time_limit:
            break
        err("Invalid time format. Use HH:MM:SS.")

    uploads: list[str] = []
    while questionary.confirm("Attach a file?", default=False, style=_STYLE).ask():
        chosen = _browse_file(str(Path.home()))
        if chosen:
            uploads.append(chosen)
            ok(f"Added: {chosen}")

    raw_cmd = questionary.text("Run command:", style=_STYLE).ask()
    if raw_cmd is None:
        return
    run_command = clean_run_command(raw_cmd)
    if not run_command.strip():
        err("Run command cannot be empty.")
        return

    raw_mods = questionary.text(
        "Modules to load (space-separated, blank=none):", default="", style=_STYLE
    ).ask()
    if raw_mods is None:
        return
    modules = clean_modules(raw_mods) if raw_mods.strip() else []

    spec = JobSpec(
        job_name=job_name, partition=partition, gpus=gpus, cpus=cpus,
        mem_gb=mem_gb, time_limit=time_limit, run_command=run_command,
        modules=modules, uploads=uploads,
    )
    folder = make_job_folder(jdir, spec)
    panel("Generated sbatch script", render_sbatch(spec, folder))

    action = questionary.select(
        "What would you like to do?",
        choices=["Submit job", "Save template only", "Discard"],
        style=_STYLE,
    ).ask()

    if action is None or action == "Discard":
        import shutil as _sh
        _sh.rmtree(folder, ignore_errors=True)
        info("Discarded.")
        return

    for src in uploads:
        if in_jail(src):
            save_upload(src, folder)
        else:
            warn(f"Skipped non-jailed upload: {src}")

    sbatch_path = write_sbatch(spec, folder)
    kv("Script saved", sbatch_path)

    if action == "Save template only":
        ok("Template saved. Not submitted.")
        auditclient.log("job_template_saved", detail=job_name)
        return

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
