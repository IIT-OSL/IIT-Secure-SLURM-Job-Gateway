# iitgpu/accounting.py
"""Usage & accounting reports (Phase 4) — sacct / sreport / sshare."""
from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass
class UsageRow:
    user: str
    cpu_hours: float
    gpu_hours: float
    job_count: int


def _run(cmd: list[str], timeout: int = 20) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _elapsed_to_hours(elapsed: str) -> float:
    """Convert SLURM elapsed ([DD-]HH:MM:SS) to hours."""
    try:
        days = 0
        if "-" in elapsed:
            d, elapsed = elapsed.split("-", 1)
            days = int(d)
        parts = [int(x) for x in elapsed.split(":")]
        while len(parts) < 3:
            parts.insert(0, 0)
        h, m, s = parts[-3], parts[-2], parts[-1]
        return days * 24 + h + m / 60 + s / 3600
    except (ValueError, IndexError):
        return 0.0


def usage_by_user(days: int = 30, all_users: bool = True) -> list[UsageRow]:
    """Aggregate CPU-hours, GPU-hours and job counts per user from sacct.

    Computes hours from Elapsed × allocated TRES (CPUs and gres/gpu), which works
    even when sreport is not configured. Newest window of `days` days.
    """
    cmd = [
        "sacct", "--noheader", "--parsable2", "-X", "-a",
        "--format=User,Elapsed,AllocTRES,State",
        "-S", f"now-{days}days",
    ]
    out = _run(cmd)
    agg: dict[str, UsageRow] = {}
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) < 3:
            continue
        user = parts[0].strip() or "?"
        if not user or user == "?":
            continue
        hours = _elapsed_to_hours(parts[1].strip())
        tres = parts[2]
        cpus = 0
        gpus = 0
        for item in tres.split(","):
            if item.startswith("cpu="):
                try: cpus = int(item[4:])
                except ValueError: pass
            elif item.startswith("gres/gpu="):
                try: gpus = int(item.split("=")[-1])
                except ValueError: pass
        row = agg.setdefault(user, UsageRow(user, 0.0, 0.0, 0))
        row.cpu_hours += hours * max(cpus, 1)
        row.gpu_hours += hours * gpus
        row.job_count += 1
    rows = sorted(agg.values(), key=lambda r: r.gpu_hours, reverse=True)
    return rows


def fairshare() -> list[tuple[str, str, str]]:
    """Return (user, raw_shares, fairshare_factor) rows from sshare."""
    out = _run(["sshare", "--noheader", "--parsable2",
                "--format=User,RawShares,FairShare"])
    rows: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) >= 3 and parts[0].strip():
            rows.append((parts[0].strip(), parts[1].strip(), parts[2].strip()))
    return rows


def sreport_cluster_usage(days: int = 30) -> str:
    """Raw sreport cluster utilization (hours), if sreport is available."""
    out = _run(["sreport", "-t", "Hours", "cluster", "AccountUtilizationByUser",
                f"start=now-{days}days", "end=now", "--parsable2"])
    return out.strip() or "sreport unavailable (slurmdbd rollups may be disabled)"


# ── TUI ─────────────────────────────────────────────────────────────────────────

def usage_menu() -> None:
    import questionary
    from questionary import Style
    from rich.table import Table
    from iitgpu.ui import console, header, info

    style = Style([("qmark", "fg:cyan bold"), ("pointer", "fg:cyan bold")])
    while True:
        header("Usage & Accounting")
        choice = questionary.select(
            "Report:",
            choices=["GPU/CPU hours per user (30d)", "Fairshare standing",
                     "Raw sreport (30d)", "Back"],
            style=style,
        ).ask()
        if choice is None or choice == "Back":
            return
        if choice.startswith("GPU/CPU"):
            rows = usage_by_user(days=30)
            if not rows:
                info("No usage in the last 30 days."); continue
            t = Table(show_header=True, header_style="bold cyan")
            for c in ("User", "GPU-hours", "CPU-hours", "Jobs"):
                t.add_column(c)
            for r in rows:
                t.add_row(r.user, f"{r.gpu_hours:.1f}", f"{r.cpu_hours:.1f}", str(r.job_count))
            console.print(t)
        elif choice == "Fairshare standing":
            rows = fairshare()
            if not rows:
                info("sshare returned nothing (fairshare may be off)."); continue
            t = Table(show_header=True, header_style="bold cyan")
            for c in ("User", "RawShares", "FairShare"):
                t.add_column(c)
            for u, raw, fs in rows:
                t.add_row(u, raw, fs)
            console.print(t)
        else:
            console.print(sreport_cluster_usage(days=30))
        questionary.press_any_key_to_continue("").ask()
