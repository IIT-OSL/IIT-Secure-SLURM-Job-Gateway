# iitgpu/dashboard.py
from __future__ import annotations

import getpass
import select
import sys
import time
from pathlib import Path

from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich import box

from iitgpu.config import load_config, jobs_dir
from iitgpu.slurm import QueueEntry, cancel, queue
from iitgpu.ui import console, err, info, ok

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False

_REFRESH_SECS = 3.0


def _get_log_tail(log_path: str, lines: int = 20) -> list[str]:
    """Return the last `lines` lines of a log file. Returns [] if file missing."""
    p = Path(log_path)
    if not p.exists():
        return []
    try:
        all_lines = p.read_text(errors="replace").splitlines()
        return all_lines[-lines:]
    except OSError:
        return []


def _find_job_log(job_id: str, search_root: str) -> str | None:
    """Search for slurm-{job_id}.out under search_root."""
    target = f"slurm-{job_id}.out"
    for p in Path(search_root).rglob(target):
        return str(p)
    return None


def _build_jobs_table(jobs: list[QueueEntry], selected_idx: int) -> Table:
    table = Table(
        show_header=True, header_style="bold cyan",
        box=box.SIMPLE, expand=True,
    )
    table.add_column("", width=2)
    table.add_column("Job ID", style="magenta", width=8)
    table.add_column("Name", width=22)
    table.add_column("State", width=12)
    table.add_column("Time", width=10)
    table.add_column("Partition", width=10)

    for i, j in enumerate(jobs):
        color = (
            "green"  if j.state == "RUNNING"   else
            "yellow" if j.state == "PENDING"   else
            "dim"
        )
        prefix = "❯" if i == selected_idx else " "
        table.add_row(
            prefix,
            j.job_id,
            j.name,
            f"[{color}]{j.state}[/]",
            j.time_used,
            j.partition,
        )
    return table


def _build_layout(
    jobs: list[QueueEntry],
    selected_idx: int,
    log_lines: list[str],
    log_path: str | None,
) -> Layout:
    layout = Layout()
    jobs_height = min(len(jobs) + 4, 12)
    layout.split_column(
        Layout(name="jobs", size=jobs_height),
        Layout(name="log"),
        Layout(name="footer", size=1),
    )

    if jobs:
        layout["jobs"].update(
            Panel(_build_jobs_table(jobs, selected_idx), title="My Jobs", border_style="cyan")
        )
    else:
        layout["jobs"].update(
            Panel("[dim]No jobs in queue.[/]", title="My Jobs", border_style="cyan")
        )

    log_title = f"Output: {log_path}" if log_path else "Output"
    log_body = "\n".join(log_lines) if log_lines else "[dim]Waiting for job to start...[/]"
    layout["log"].update(Panel(log_body, title=log_title, border_style="cyan"))

    layout["footer"].update(
        "[dim]  Q=quit   S=switch job   C=cancel selected   R=refresh now[/]"
    )
    return layout


def _wait_key(timeout: float) -> str | None:
    """Wait up to `timeout` seconds for a keypress. Returns char (lower) or None."""
    if not _HAS_TERMIOS:
        time.sleep(timeout)
        return None
    try:
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if r:
            return sys.stdin.read(1).lower()
    except (OSError, ValueError):
        pass
    return None


def run_dashboard(job_id: str | None = None) -> None:
    """Show the live job dashboard. If job_id given, start with that job selected."""
    cfg = load_config()
    user = getpass.getuser()
    jdir = jobs_dir(cfg)

    jobs: list[QueueEntry] = queue(user=user)
    selected_idx = 0

    if job_id is not None:
        for i, j in enumerate(jobs):
            if j.job_id == job_id:
                selected_idx = i
                break

    old_settings = None
    if _HAS_TERMIOS and sys.stdin.isatty():
        try:
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            old_settings = None

    try:
        with Live(console=console, refresh_per_second=1, screen=False) as live:
            while True:
                selected_job = jobs[selected_idx] if jobs and selected_idx < len(jobs) else None
                log_path: str | None = None
                if selected_job:
                    log_path = _find_job_log(selected_job.job_id, jdir)

                log_lines = _get_log_tail(log_path, lines=20) if log_path else []
                live.update(_build_layout(jobs, selected_idx, log_lines, log_path))

                key = _wait_key(_REFRESH_SECS)

                if key == "q":
                    break
                elif key == "s" and jobs:
                    selected_idx = (selected_idx + 1) % len(jobs)
                elif key == "c" and selected_job:
                    live.stop()
                    import questionary
                    from questionary import Style
                    _s = Style([("question", "bold"), ("answer", "fg:magenta bold")])
                    if questionary.confirm(
                        f"Cancel job {selected_job.job_id} ({selected_job.name})?",
                        default=False, style=_s,
                    ).ask():
                        success, msg = cancel(selected_job.job_id)
                        (ok if success else err)(msg)
                    live.start()

                # Refresh job list
                jobs = queue(user=user)
                if jobs and selected_idx >= len(jobs):
                    selected_idx = len(jobs) - 1

    finally:
        if old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass
