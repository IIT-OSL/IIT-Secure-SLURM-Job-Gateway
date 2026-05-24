# iitgpu/monitor.py
import getpass
from pathlib import Path

import questionary
from questionary import Style
from rich.table import Table

from iitgpu import auditclient
from iitgpu.slurm import cancel, queue
from iitgpu.ui import console, err, header, info, kv, ok, warn
from iitgpu.validate import in_jail, safe_listdir

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
])


def show_queue() -> None:
    header("My Job Queue")
    entries = queue(user=getpass.getuser())
    if not entries:
        info("No jobs in queue.")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Job ID", style="magenta")
    table.add_column("Name")
    table.add_column("State", style="cyan")
    table.add_column("Partition")
    table.add_column("Time Used")
    table.add_column("Nodes")
    for e in entries:
        s = "green" if e.state == "RUNNING" else "yellow" if e.state == "PENDING" else "red"
        table.add_row(e.job_id, e.name, f"[{s}]{e.state}[/]", e.partition, e.time_used, str(e.nodes))
    console.print(table)


def cancel_job() -> None:
    header("Cancel Job")
    entries = queue(user=getpass.getuser())
    if not entries:
        info("No jobs to cancel.")
        return
    choices = [f"{e.job_id}  {e.name}  [{e.state}]" for e in entries] + ["[back]"]
    choice = questionary.select("Select job to cancel:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[back]":
        return
    job_id = choice.split()[0]
    if not questionary.confirm(f"Cancel job {job_id}?", default=False, style=_STYLE).ask():
        return
    auditclient.log("job_cancel", detail="user_requested", job_id=job_id)
    success, msg = cancel(job_id)
    (ok if success else err)(msg)


def tail_log(log_path: str, lines: int = 50) -> None:
    if not in_jail(log_path):
        err("Access denied: log path is outside allowed directories.")
        return
    p = Path(log_path)
    if not p.exists():
        warn(f"Log file not found: {log_path}")
        return
    header(f"Log: {p.name}")
    try:
        text = p.read_text(errors="replace")
        for line in text.splitlines()[-lines:]:
            console.print(line)
    except OSError as exc:
        err(f"Cannot read log: {exc}")


def browse_and_tail_log() -> None:
    header("View Job Log")
    from iitgpu.config import load_config, jobs_dir
    cfg = load_config()
    user_dir = str(Path(jobs_dir(cfg)) / getpass.getuser())
    folders = safe_listdir(user_dir)
    if not folders:
        info("No job folders found.")
        return
    choice = questionary.select(
        "Select job folder:", choices=sorted(folders, reverse=True) + ["[back]"], style=_STYLE
    ).ask()
    if choice is None or choice == "[back]":
        return
    job_folder = str(Path(user_dir) / choice)
    if not in_jail(job_folder):
        err("Access denied.")
        return
    logs = [f for f in safe_listdir(job_folder) if f.endswith(".out") or f.endswith(".err")]
    if not logs:
        info("No log files in that folder.")
        return
    log_choice = questionary.select("Select log file:", choices=logs + ["[back]"], style=_STYLE).ask()
    if log_choice is None or log_choice == "[back]":
        return
    tail_log(str(Path(job_folder) / log_choice))


def cluster_status() -> None:
    from iitgpu.slurm import get_partitions
    header("Cluster Status")
    partitions = get_partitions()
    if not partitions:
        warn("Could not retrieve partition info.")
        return
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Partition", style="magenta")
    table.add_column("State", style="cyan")
    table.add_column("Nodes")
    table.add_column("GPUs/Node")
    for p in partitions:
        s = "green" if p.state == "up" else "red"
        table.add_row(p.name, f"[{s}]{p.state}[/]", str(p.nodes), str(p.gpus_per_node))
    console.print(table)


def monitor_menu() -> None:
    while True:
        header("Monitor")
        choice = questionary.select(
            "Monitor options:",
            choices=["View my queue", "Cancel a job", "View job log", "Back to main menu"],
            style=_STYLE,
        ).ask()
        if choice is None or choice == "Back to main menu":
            return
        if choice == "View my queue":
            show_queue()
        elif choice == "Cancel a job":
            cancel_job()
        elif choice == "View job log":
            browse_and_tail_log()
