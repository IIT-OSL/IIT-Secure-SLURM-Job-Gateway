# iitgpu/workspace.py
"""Unified workspace view — files, models, environments, and recent jobs."""
from __future__ import annotations

import getpass
import os
import shutil as _shutil
from pathlib import Path

import questionary
from questionary import Style
from rich.panel import Panel
from rich.table import Table

from iitgpu.config import load_config, jobs_dir, models_dir
from iitgpu.ui import console, header, info, warn

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
])


def _fmt_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes //= 1024
    return f"{nbytes:.1f} PB"


def _dir_size(path: Path) -> int:
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def _count_items(path: Path) -> int:
    try:
        return sum(1 for _ in path.iterdir())
    except OSError:
        return 0


def _recent_jobs(user_jobs_dir: str, n: int = 5) -> list[tuple[str, str]]:
    """Return up to n most recent job folders with a state guess."""
    p = Path(user_jobs_dir)
    if not p.exists():
        return []
    folders = sorted(
        (d for d in p.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
        reverse=True,
    )[:n]
    result = []
    for folder in folders:
        err_files = list(folder.glob("*.err"))
        if not err_files:
            state = "UNKNOWN"
        elif any(f.stat().st_size > 0 for f in err_files):
            state = "FAILED"
        else:
            out_files = list(folder.glob("*.out"))
            state = "COMPLETED" if out_files else "PENDING"
        result.append((folder.name, state))
    return result


def _disk_usage_summary(nfs_root: str) -> tuple[int, int]:
    """Return (used_bytes, total_bytes) for the NFS root filesystem."""
    try:
        st = _shutil.disk_usage(nfs_root)
        return st.used, st.total
    except OSError:
        return 0, 0


def run_workspace() -> None:
    cfg = load_config()
    user = getpass.getuser()
    nfs = cfg.nfs_root
    user_dir = Path(nfs) / user
    jdir = jobs_dir(cfg)
    mdir = models_dir(cfg)

    while True:
        header("My Workspace")

        # ── Disk summary ──────────────────────────────────────────────────────
        used, total = _disk_usage_summary(nfs)
        free = total - used
        if total > 0:
            console.print(
                f"[bold]Disk:[/] {_fmt_size(free)} free of {_fmt_size(total)}   ({nfs})\n"
            )

        # ── My Files table ────────────────────────────────────────────────────
        files_table = Table(show_header=False, box=None, padding=(0, 2))
        files_table.add_column("Name", style="cyan")
        files_table.add_column("Items")
        files_table.add_column("Size", justify="right")

        for subdir in ("datasets", "data", "models", "scripts"):
            p = user_dir / subdir
            if p.exists():
                n = _count_items(p)
                sz = _dir_size(p)
                files_table.add_row(f"{subdir}/", f"{n} item{'s' if n != 1 else ''}", _fmt_size(sz))

        # Recent jobs
        recent = _recent_jobs(str(Path(jdir) / user))
        if recent:
            for name, state in recent:
                sc = "green" if state == "COMPLETED" else "red" if state == "FAILED" else "yellow"
                files_table.add_row(f"jobs/{name}", f"[{sc}]{state}[/{sc}]", "")

        console.print(Panel(files_table, title="[bold]My Files[/]", expand=False))

        # ── Environments ──────────────────────────────────────────────────────
        try:
            from iitgpu.envs import list_all_envs
            envs = list_all_envs(cfg)
        except Exception:
            envs = []

        if envs:
            env_table = Table(show_header=False, box=None, padding=(0, 2))
            env_table.add_column("Name", style="cyan")
            env_table.add_column("Kind")
            env_table.add_column("Path", style="dim")
            for e in envs:
                env_table.add_row(e.name, f"({e.kind})", e.path)
            console.print(Panel(env_table, title="[bold]Environments[/]", expand=False))

        # ── Downloaded Models ─────────────────────────────────────────────────
        try:
            from iitgpu.models import load_registry
            registry = load_registry(cfg)
        except Exception:
            registry = []

        if registry:
            mdl_table = Table(show_header=False, box=None, padding=(0, 2))
            mdl_table.add_column("Name", style="cyan")
            mdl_table.add_column("Size", justify="right")
            mdl_table.add_column("Source", style="dim")
            for m in registry:
                mdl_table.add_row(m.name, f"{m.size_mb:.1f} MB", m.source)
            console.print(Panel(mdl_table, title="[bold]Downloaded Models[/]", expand=False))

        # ── Actions ───────────────────────────────────────────────────────────
        choice = questionary.select(
            "Action:",
            choices=[
                "Browse my files",
                "Upload data",
                "Download a model",
                "Build / manage environments",
                "Delete a model",
                "Back to main menu",
            ],
            style=_STYLE,
        ).ask()

        if choice is None or choice == "Back to main menu":
            return
        elif choice == "Browse my files":
            from iitgpu.files import file_manager
            file_manager()
        elif choice == "Upload data":
            from iitgpu.upload import run_upload
            run_upload()
        elif choice == "Download a model":
            from iitgpu.models import model_menu
            model_menu(cfg)
        elif choice == "Build / manage environments":
            from iitgpu.setup import _run_env_setup, _run_install_prebuilt
            _run_env_setup(cfg)
            _run_install_prebuilt(cfg)
        elif choice == "Delete a model":
            from iitgpu.models import _remove_interactive
            _remove_interactive(cfg)
