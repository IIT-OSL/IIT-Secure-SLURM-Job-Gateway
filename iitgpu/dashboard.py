# iitgpu/dashboard.py
from __future__ import annotations

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
from iitgpu.slurm import NodeStats, QueueEntry, cancel, get_node_stats, queue, recent_jobs
from iitgpu.ui import console, err, ok

try:
    import termios
    import tty
    _HAS_TERMIOS = True
except ImportError:
    _HAS_TERMIOS = False

_DATA_REFRESH_SECS = 2.0
_DISPLAY_FPS       = 4
_COMPLETED_HISTORY = 2
_SPINNERS          = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


# ── Time helpers ──────────────────────────────────────────────────────────────

def _slurm_time_to_secs(t: str) -> int | None:
    if not t or t in ("N/A", "UNLIMITED", "NOT_SET", "Partition_Limit", "-"):
        return None
    try:
        days = 0
        if "-" in t:
            d, t = t.split("-", 1)
            days = int(d)
        parts = t.split(":")
        if len(parts) == 3:
            return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        if len(parts) == 2:
            return days * 86400 + int(parts[0]) * 60 + int(parts[1])
        return None
    except (ValueError, IndexError):
        return None


def _fmt_duration(secs: int) -> str:
    m, s = divmod(abs(secs), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ── Bar helper (shared by cluster panel and hardware stats) ───────────────────

def _hw_bar(pct: float, width: int = 22) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    color = "red" if pct >= 90 else "yellow" if pct >= 70 else "green"
    return f"[{color}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


# ── Log helpers ───────────────────────────────────────────────────────────────

def _get_log_tail(log_path: str, lines: int = 20) -> list[str]:
    p = Path(log_path)
    if not p.exists():
        return []
    try:
        all_lines = p.read_text(errors="replace").splitlines()
        return all_lines[-lines:]
    except OSError:
        return []


def _find_job_log(job_id: str, search_root: str) -> str | None:
    target = f"slurm-{job_id}.out"
    for p in Path(search_root).rglob(target):
        return str(p)
    return None


def _get_job_output(job_id: str, jdir: str, lines: int = 20) -> tuple[list[str], str | None]:
    log_path = _find_job_log(job_id, jdir)
    if log_path is None:
        return [], None
    out_lines = _get_log_tail(log_path, lines=lines)
    err_path = str(Path(log_path).with_suffix(".err"))
    err_lines = _get_log_tail(err_path, lines=15)
    if err_lines:
        combined = err_lines + (["", "[dim]── stdout ──[/dim]"] + out_lines if out_lines else [])
    else:
        combined = out_lines
    return combined, log_path


# ── Cluster panel (compact summary bar) ──────────────────────────────────────

def _build_cluster_panel(stats: NodeStats | None) -> Panel:
    if stats is None:
        body = "[dim]Cluster stats unavailable[/]"
    else:
        state = stats.state.split("+")[0]
        sc = "green" if "IDLE" in state else "yellow" if "ALLOC" in state else "red"

        if stats.live_stats:
            gpu_color = "red" if stats.gpu_util >= 90 else "yellow" if stats.gpu_util >= 70 else "green"
            gpu_str = (
                f"GPU [bold {gpu_color}]{stats.gpu_util}%[/] "
                f"[dim]{stats.gpu_mem_used_mb/1024:.1f}/{stats.gpu_mem_total_mb/1024:.0f}GB "
                f"{stats.gpu_temp}°C {stats.gpu_power_w:.0f}W[/]"
            )
            cpu_color = "red" if stats.cpu_util >= 90 else "yellow" if stats.cpu_util >= 70 else "green"
            cpu_str = f"CPU [bold {cpu_color}]{stats.cpu_util}%[/] [dim]load {stats.cpu_load:.2f}[/]"
            mem_pct = stats.mem_used_mb / stats.mem_total_mb * 100 if stats.mem_total_mb else 0
            mem_color = "red" if mem_pct >= 90 else "yellow" if mem_pct >= 70 else "green"
            mem_str = f"RAM [bold {mem_color}]{stats.mem_used_mb/1024:.0f}/{stats.mem_total_mb/1024:.0f} GB[/]"
        else:
            gpu_color = "yellow" if stats.gpu_alloc > 0 else "green"
            gpu_str = f"GPU [bold {gpu_color}]{stats.gpu_alloc}/{stats.gpu_total} alloc[/]"
            cpu_str = f"CPU [bold]{stats.cpu_alloc}/{stats.cpu_total}[/] [dim]load {stats.cpu_load:.2f}[/]"
            mem_str = f"RAM [bold]{stats.mem_alloc_mb//1024}/{stats.mem_total_mb//1024} GB[/] [dim]alloc[/]"

        body = f"  iit-MS-7E06  [{sc}]{state}[/]  │  {gpu_str}  │  {cpu_str}  │  {mem_str}"

    return Panel(body, title="[bold]Cluster: iit[/bold]", border_style="blue", height=3)


# ── Jobs table ────────────────────────────────────────────────────────────────

def _build_jobs_table(jobs: list[QueueEntry], selected_idx: int) -> Table:
    table = Table(
        show_header=True, header_style="bold cyan",
        box=box.SIMPLE, expand=True, show_edge=False,
    )
    table.add_column("",        width=2)
    table.add_column("ID",      style="magenta", width=7,  no_wrap=True)
    table.add_column("User",    width=8,  no_wrap=True)
    table.add_column("Name",    width=22, no_wrap=True)
    table.add_column("State",   width=14, no_wrap=True)
    table.add_column("Elapsed", width=9,  no_wrap=True)
    table.add_column("Part",    width=5,  no_wrap=True)

    spin = _SPINNERS[int(time.monotonic() * _DISPLAY_FPS) % len(_SPINNERS)]
    added_done_sep = False

    for i, j in enumerate(jobs):
        is_done = j.state in ("COMPLETED", "FAILED", "CANCELLED")
        is_selected = i == selected_idx

        if is_done and not added_done_sep:
            added_done_sep = True
            table.add_row(
                "", "[dim]──[/]", "[dim]──────[/]",
                "[dim]─── recent ──────────────[/]",
                "[dim]────────────[/]", "[dim]───────[/]", "[dim]───[/]",
            )

        prefix = "[bold cyan]❯[/]" if is_selected else " "

        if is_done:
            s_color = "cyan" if j.state == "COMPLETED" else "red"
            elapsed_secs = _slurm_time_to_secs(j.time_used) or 0
            elapsed_str = _fmt_duration(elapsed_secs) if elapsed_secs > 0 else j.time_used
            table.add_row(
                prefix,
                f"[dim strike]{j.job_id}[/]",
                f"[dim strike]{j.user[:7]}[/]",
                f"[dim strike]{j.name[:21]}[/]",
                f"[{s_color} strike]{j.state}[/]",
                f"[dim strike]{elapsed_str}[/]",
                f"[dim strike]{j.partition}[/]",
            )
        elif j.state in ("RUNNING", "COMPLETING"):
            label = "RUNNING" if j.state == "RUNNING" else "FINISHING"
            table.add_row(
                prefix,
                j.job_id,
                j.user[:7],
                j.name[:21],
                f"[green]{spin} {label}[/]",
                f"[dim]{j.time_used}[/]",
                f"[dim]{j.partition}[/]",
            )
        elif j.state == "PENDING":
            table.add_row(
                prefix,
                j.job_id,
                j.user[:7],
                j.name[:21],
                "[yellow]⋯ PENDING[/]",
                "[dim]─[/]",
                f"[dim]{j.partition}[/]",
            )
        else:
            table.add_row(
                prefix,
                j.job_id,
                j.user[:7],
                j.name[:21],
                f"[dim]{j.state}[/]",
                f"[dim]{j.time_used}[/]",
                f"[dim]{j.partition}[/]",
            )

    return table


# ── Dashboard layout ──────────────────────────────────────────────────────────

def _build_layout(
    jobs: list[QueueEntry],
    selected_idx: int,
    log_lines: list[str],
    log_path: str | None,
    node_stats: NodeStats | None,
) -> Layout:
    layout = Layout()
    jobs_height = min(len(jobs) + 6, 16)

    layout.split_column(
        Layout(name="cluster", size=3),
        Layout(name="jobs",    size=jobs_height),
        Layout(name="log"),
        Layout(name="footer",  size=1),
    )

    layout["cluster"].update(_build_cluster_panel(node_stats))

    if jobs:
        layout["jobs"].update(
            Panel(_build_jobs_table(jobs, selected_idx),
                  title="[bold]Job Queue[/bold]", border_style="cyan")
        )
    else:
        layout["jobs"].update(
            Panel("[dim]No jobs in queue or history.[/]",
                  title="[bold]Job Queue[/bold]", border_style="cyan")
        )

    selected_job = jobs[selected_idx] if jobs and selected_idx < len(jobs) else None
    log_title = f"Output: {log_path}" if log_path else "Output"
    if log_lines:
        log_body = "\n".join(log_lines)
    elif selected_job is None:
        log_body = "[dim]No job selected.[/]"
    elif selected_job.state == "FAILED":
        log_body = "[red]Job failed — output not yet visible. Press R to refresh.[/]"
    elif selected_job.state == "COMPLETED":
        log_body = "[dim]Job completed — output not yet visible. Press R to refresh.[/]"
    else:
        log_body = "[dim]Waiting for job to start...[/]"

    layout["log"].update(Panel(log_body, title=log_title, border_style="cyan"))
    layout["footer"].update(
        "[dim]  Q=quit   S=switch job   C=cancel selected   R=refresh now[/]"
    )
    return layout


# ── Hardware stats view ───────────────────────────────────────────────────────

def _build_hw_panel(stats: NodeStats | None) -> Panel:
    lines = [""]

    if stats is None:
        lines.append("  [dim]SLURM node unavailable[/]")
    elif stats.live_stats:
        # ── GPU ───────────────────────────────────────────────────────────
        gpu_pct   = float(stats.gpu_util)
        vram_pct  = stats.gpu_mem_used_mb / stats.gpu_mem_total_mb * 100 if stats.gpu_mem_total_mb else 0
        lines.append("  [bold]GPU[/bold]")
        lines.append(
            f"  Utilization   {_hw_bar(gpu_pct)}"
            f"  [bold]{gpu_pct:3.0f}%[/bold]"
            f"   [dim]{stats.gpu_temp}°C  {stats.gpu_power_w:.0f} W[/dim]"
        )
        lines.append(
            f"  VRAM          {_hw_bar(vram_pct)}"
            f"  [bold]{stats.gpu_mem_used_mb/1024:.1f} / {stats.gpu_mem_total_mb/1024:.0f} GB[/bold]"
        )
        lines.append("")

        # ── CPU ───────────────────────────────────────────────────────────
        cpu_pct = float(stats.cpu_util)
        lines.append(f"  [bold]CPU[/bold]  [dim]({stats.cpu_total} cores)[/dim]")
        lines.append(
            f"  Utilization   {_hw_bar(cpu_pct)}"
            f"  [bold]{cpu_pct:3.0f}%[/bold]"
        )
        lines.append(
            f"  Load avg      [dim]{stats.cpu_load:.2f}  ·  {stats.cpu_load5:.2f}[/dim]"
            f"   [dim](1 / 5 min)[/dim]"
        )
        lines.append("")

        # ── RAM ───────────────────────────────────────────────────────────
        mem_pct = stats.mem_used_mb / stats.mem_total_mb * 100 if stats.mem_total_mb else 0
        lines.append("  [bold]RAM[/bold]")
        lines.append(
            f"  Used          {_hw_bar(mem_pct)}"
            f"  [bold]{stats.mem_used_mb/1024:.1f} / {stats.mem_total_mb/1024:.0f} GB[/bold]"
            f"  [dim]({mem_pct:.0f}%)[/dim]"
        )
    else:
        lines.append("  [yellow]Live stats unavailable[/yellow]  "
                     "[dim]— iit-gpu-stats-writer not running on iit-MS-7E06[/dim]")
        lines.append("")
        lines.append("  [dim]Start it with:  python3 /usr/local/bin/iit-gpu-stats-writer &[/dim]")

    lines.append("")

    # ── SLURM allocation ──────────────────────────────────────────────────────
    if stats:
        node_state = stats.state.split("+")[0]
        sc = "green" if "IDLE" in node_state else "yellow" if "ALLOC" in node_state else "red"
        alloc_parts = [
            f"GPU {stats.gpu_alloc}/{stats.gpu_total}",
            f"CPU {stats.cpu_alloc}/{stats.cpu_total}",
            f"RAM {stats.mem_alloc_mb//1024}/{stats.mem_total_mb//1024} GB",
        ]
        lines.append(
            f"  [bold]SLURM[/bold]   [{sc}]{node_state}[/]  ·  "
            + "  ·  ".join(alloc_parts)
        )

    lines.append("")
    return Panel("\n".join(lines), title="[bold]Hardware Stats: iit-MS-7E06[/bold]", border_style="blue")


def run_hardware_stats() -> None:
    """Live hardware utilization view: GPU, CPU, RAM, SLURM. Q to quit."""
    _stats:   list[NodeStats | None] = [None]
    _last_ts: list[float]            = [0.0]

    def _refresh() -> None:
        _stats[0]   = get_node_stats()
        _last_ts[0] = time.monotonic()

    _refresh()

    old_settings = None
    if _HAS_TERMIOS and sys.stdin.isatty():
        try:
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            old_settings = None

    try:
        with Live(console=console, refresh_per_second=_DISPLAY_FPS, screen=True) as live:
            while True:
                live.update(_build_hw_panel(_stats[0]))

                key = None
                if _HAS_TERMIOS:
                    try:
                        r, _, _ = select.select([sys.stdin], [], [], 1.0 / _DISPLAY_FPS)
                        if r:
                            key = sys.stdin.read(1).lower()
                    except (OSError, ValueError):
                        pass
                else:
                    time.sleep(1.0 / _DISPLAY_FPS)

                if key == "q":
                    break

                if time.monotonic() - _last_ts[0] >= _DATA_REFRESH_SECS:
                    _refresh()
    finally:
        if old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass


# ── Keyboard ──────────────────────────────────────────────────────────────────

def _wait_key(timeout: float) -> str | None:
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


# ── Merged job list ───────────────────────────────────────────────────────────

def _merged_jobs(jdir: str) -> list[QueueEntry]:
    live = queue()
    live_ids = {j.job_id for j in live}
    done = [j for j in recent_jobs(jdir, limit=_COMPLETED_HISTORY) if j.job_id not in live_ids]
    return live + done


# ── Main dashboard ────────────────────────────────────────────────────────────

def run_dashboard(job_id: str | None = None) -> None:
    """Show the live job dashboard. If job_id given, start with that job selected."""
    cfg = load_config()
    jdir = jobs_dir(cfg)

    jobs: list[QueueEntry] = _merged_jobs(jdir)
    selected_idx = 0
    pinned_job_id: str | None = job_id

    if job_id is not None:
        for i, j in enumerate(jobs):
            if j.job_id == job_id:
                selected_idx = i
                break

    _node_stats:   list[NodeStats | None] = [None]
    _log_lines:    list[list[str]]        = [[]]
    _log_path_ref: list[str | None]       = [None]
    _last_data_ts: list[float]            = [0.0]

    def _refresh_data() -> None:
        nonlocal jobs, selected_idx
        _node_stats[0] = get_node_stats()
        jobs = _merged_jobs(jdir)
        if jobs and selected_idx >= len(jobs):
            selected_idx = len(jobs) - 1
        sel = jobs[selected_idx] if jobs and selected_idx < len(jobs) else None
        lookup_id = sel.job_id if sel else pinned_job_id
        if lookup_id:
            lines, path = _get_job_output(lookup_id, jdir)
        else:
            lines, path = [], None
        _log_lines[0]    = lines
        _log_path_ref[0] = path
        _last_data_ts[0] = time.monotonic()

    _refresh_data()

    old_settings = None
    if _HAS_TERMIOS and sys.stdin.isatty():
        try:
            old_settings = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
        except termios.error:
            old_settings = None

    try:
        with Live(console=console, refresh_per_second=_DISPLAY_FPS, screen=True) as live:
            while True:
                live.update(_build_layout(
                    jobs, selected_idx,
                    _log_lines[0], _log_path_ref[0],
                    _node_stats[0],
                ))

                key = _wait_key(1.0 / _DISPLAY_FPS)

                if key == "q":
                    break
                elif key == "s" and jobs:
                    selected_idx = (selected_idx + 1) % len(jobs)
                    _refresh_data()
                elif key == "c":
                    sel = jobs[selected_idx] if jobs and selected_idx < len(jobs) else None
                    if sel:
                        live.stop()
                        import questionary
                        from questionary import Style
                        _s = Style([("question", "bold"), ("answer", "fg:magenta bold")])
                        if questionary.confirm(
                            f"Cancel job {sel.job_id} ({sel.name})?",
                            default=False, style=_s,
                        ).ask():
                            success, msg = cancel(sel.job_id)
                            (ok if success else err)(msg)
                            if success:
                                from iitgpu import auditclient as _audit
                                _audit.log("job_cancel", detail="dashboard", job_id=sel.job_id)
                        live.start()
                elif key == "r":
                    _refresh_data()

                if time.monotonic() - _last_data_ts[0] >= _DATA_REFRESH_SECS:
                    _refresh_data()

    finally:
        if old_settings is not None:
            try:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
            except termios.error:
                pass
