# iitgpu/monitor.py
import getpass
from pathlib import Path

import questionary
from questionary import Style
from rich.table import Table

from iitgpu import auditclient
from iitgpu.slurm import (cancel, hold, release, requeue, queue,
                          job_detail, job_efficiency, filtered_history)
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
    """Back-compat alias — opens the full job-management menu."""
    manage_job()


def manage_job() -> None:
    header("Manage Job")
    entries = queue(user=getpass.getuser())
    if not entries:
        info("No active jobs.")
        return
    choices = [f"{e.job_id}  {e.name}  [{e.state}]" for e in entries] + ["[back]"]
    choice = questionary.select("Select a job:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[back]":
        return
    job_id = choice.split()[0]

    action = questionary.select(
        f"Action for job {job_id}:",
        choices=["Cancel", "Hold", "Release", "Requeue", "Details + efficiency", "[back]"],
        style=_STYLE,
    ).ask()
    if action is None or action == "[back]":
        return

    if action == "Details + efficiency":
        from iitgpu.ui import console
        header(f"Job {job_id} detail")
        console.print(job_detail(job_id))
        console.print()
        console.print("[bold cyan]── Efficiency (seff) ──[/]")
        console.print(job_efficiency(job_id))
        questionary.press_any_key_to_continue("").ask()
        return

    _ACTIONS = {
        "Cancel":  (cancel,  "job_cancel"),
        "Hold":    (hold,    "job_hold"),
        "Release": (release, "job_release"),
        "Requeue": (requeue, "job_requeue"),
    }
    fn, audit_action = _ACTIONS[action]
    if action in ("Cancel", "Requeue") and not questionary.confirm(
        f"{action} job {job_id}?", default=False, style=_STYLE
    ).ask():
        return
    auditclient.log(audit_action, detail="user_requested", job_id=job_id)
    success, msg = fn(job_id)
    (ok if success else err)(msg)


def tail_log(log_path: str, lines: int | None = None) -> None:
    """Display a job log.

    By default the FULL log is shown through a pager (less) so it can be
    scrolled and searched with `/` — important for analyzing failures that
    happen early in the run (e.g. an import traceback at the top, which a
    bottom-only tail would hide). Pass an int to show only the last N lines.
    """
    if not in_jail(log_path):
        err("Access denied: log path is outside allowed directories.")
        return
    p = Path(log_path)
    if not p.exists():
        warn(f"Log file not found: {log_path}")
        return
    try:
        text = p.read_text(errors="replace")
    except OSError as exc:
        err(f"Cannot read log: {exc}")
        return

    all_lines = text.splitlines()
    if lines is not None:
        header(f"Log: {p.name}  (last {min(lines, len(all_lines))} of {len(all_lines)} lines)")
        for line in all_lines[-lines:]:
            console.print(line, markup=False, highlight=False)
        return

    # Full log via pager so the whole thing is scrollable + searchable.
    header(f"Log: {p.name}  ({len(all_lines)} lines)  —  arrows/PgUp to scroll, '/' to search, 'q' to quit")
    with console.pager(styles=False):
        # markup/highlight off: log text (tracebacks, "[Errno 13]", etc.) must
        # render literally and not be interpreted as Rich markup.
        for line in all_lines:
            console.print(line, markup=False, highlight=False)


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
            choices=[
                "View my queue",
                "Cancel a job",
                "View job log",
                "View hardware stats",
                "Back to main menu",
            ],
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
        elif choice == "View hardware stats":
            from iitgpu.dashboard import run_hardware_stats
            run_hardware_stats()


def follow_log() -> None:
    """Live-follow a running job's output (like tail -f). Ctrl-C to stop."""
    import time
    from iitgpu.config import load_config, jobs_dir
    cfg = load_config()
    user_dir = str(Path(jobs_dir(cfg)) / getpass.getuser())
    folders = safe_listdir(user_dir)
    if not folders:
        info("No job folders found.")
        return
    choice = questionary.select(
        "Follow which job folder?", choices=sorted(folders, reverse=True) + ["[back]"], style=_STYLE
    ).ask()
    if choice is None or choice == "[back]":
        return
    folder = str(Path(user_dir) / choice)
    if not in_jail(folder):
        err("Access denied."); return
    logs = [f for f in safe_listdir(folder) if f.endswith(".out")]
    if not logs:
        info("No .out file yet."); return
    log_path = str(Path(folder) / sorted(logs)[0])
    header(f"Following {Path(log_path).name}  (Ctrl-C to stop)")
    try:
        pos = 0
        for _ in range(100000):  # bounded so it can't run forever in a TUI
            try:
                with open(log_path, "r", errors="replace") as fh:
                    fh.seek(pos)
                    chunk = fh.read()
                    pos = fh.tell()
                if chunk:
                    console.print(chunk, end="")
            except OSError:
                pass
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[dim]— stopped following —[/]")


def show_history() -> None:
    """Completed-job history with state filter and user scope."""
    from iitgpu.config import load_config, jobs_dir, is_admin
    from rich.table import Table
    cfg = load_config()
    header("Job History")
    state = questionary.select(
        "Filter by state:",
        choices=["All", "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"], style=_STYLE,
    ).ask()
    if state is None:
        return
    all_users = False
    if is_admin(cfg):
        all_users = questionary.confirm("Show ALL users (admin)?", default=False, style=_STYLE).ask()
    rows = filtered_history(jobs_dir(cfg), limit=50,
                            state=None if state == "All" else state,
                            all_users=all_users)
    if not rows:
        info("No matching history."); return
    table = Table(show_header=True, header_style="bold cyan")
    for col in ("Job ID", "User", "Name", "State", "Elapsed"):
        table.add_column(col)
    for e in rows:
        sc = "green" if e.state == "COMPLETED" else "red" if e.state in ("FAILED","TIMEOUT") else "yellow"
        table.add_row(e.job_id, e.user, e.name, f"[{sc}]{e.state}[/]", e.time_used)
    console.print(table)
