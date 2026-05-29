# iitgpu/menu.py
import questionary
from questionary import Style

from iitgpu.config import load_config, jobs_dir
from iitgpu.ui import header, info, kv

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
])

_ITEMS = [
    "1. Upload files   (store datasets in /shared for jobs)",
    "2. Setup          (environment, data, model, health check)",
    "3. Run a job      (submit ML training / inference job)",
    "4. Monitor        (live dashboard, job queue, logs)",
    "5. Advanced       (SLURM command shell)",
    "6. Quit",
]


def _show_settings() -> None:
    header("Settings (Read-Only)")
    cfg = load_config()
    kv("NFS_ROOT", cfg.nfs_root)
    kv("JOBS_SUBDIR", cfg.jobs_subdir)
    kv("DEMO_MODE", str(cfg.demo_mode))
    kv("Jobs directory", jobs_dir(cfg))
    from iitgpu.config import models_dir, templates_dir
    kv("Models directory", models_dir(cfg))
    kv("Templates directory", templates_dir(cfg))
    info("[dim]Settings are controlled by your admin via environment variables.[/]")


def run_menu() -> None:
    while True:
        header("Main Menu")
        choice = questionary.select(
            "Select an option:", choices=_ITEMS, style=_STYLE
        ).ask()

        if choice is None or choice.startswith("6."):
            info("Goodbye.")
            return

        elif choice.startswith("1."):
            from iitgpu.upload import run_upload
            run_upload()

        elif choice.startswith("2."):
            from iitgpu.setup import run_setup
            run_setup()

        elif choice.startswith("3."):
            from iitgpu.wizard import run_wizard
            run_wizard()

        elif choice.startswith("4."):
            _monitor_menu()

        elif choice.startswith("5."):
            from iitgpu.shell import run_shell
            run_shell()


def _monitor_menu() -> None:
    from iitgpu.dashboard import run_dashboard
    from iitgpu.monitor import show_queue, cancel_job, browse_and_tail_log

    while True:
        header("Monitor")
        choice = questionary.select(
            "Monitor options:",
            choices=[
                "Live dashboard  (auto-refresh)",
                "View my queue",
                "Cancel a job",
                "View job log",
                "Cluster status",
                "Back to main menu",
            ],
            style=_STYLE,
        ).ask()

        if choice is None or choice == "Back to main menu":
            return
        elif "Live dashboard" in choice:
            run_dashboard()
        elif choice == "View my queue":
            show_queue()
        elif choice == "Cancel a job":
            cancel_job()
        elif choice == "View job log":
            browse_and_tail_log()
        elif choice == "Cluster status":
            _show_cluster_status()


def _show_cluster_status() -> None:
    from iitgpu.slurm import get_partitions
    from iitgpu.ui import console, warn
    from rich.table import Table

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
