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

_MAIN_ITEMS = [
    "1. New Job       (environment + data + model + script + submit)",
    "2. My Workspace  (files, models, environments)",
    "3. Jobs          (queue, history, logs, rerun)",
    "4. Settings      (health check, shell, cluster status, hardware)",
    "5. Quit",
]


def run_menu() -> None:
    from iitgpu.config import load_config as _lc, is_admin as _ia
    _admin = _ia(_lc())
    while True:
        header("Main Menu")

        # Maintenance banner
        _maint = None
        try:
            from iitgpu.admin import get_maintenance
            _maint = get_maintenance()
        except Exception:
            pass
        if _maint:
            from iitgpu.ui import console
            from rich.panel import Panel
            _body = (
                "[bold yellow]MAINTENANCE[/]  "
                + _maint.get("reason", "") + "\n"
                + "[dim]Set by " + _maint.get("set_by", "?") + " at "
                + _maint.get("since", "")[:19] + " UTC[/]"
            )
            console.print(Panel(_body, border_style="yellow", expand=False))
            if not _admin:
                info("[dim]The cluster is currently unavailable. Please try again later.[/]")
                questionary.select(
                    "Select an option:", choices=["Quit"], style=_STYLE
                ).ask()
                info("Goodbye.")
                return

        _choices = list(_MAIN_ITEMS)
        if _admin:
            _choices.insert(len(_choices) - 1,
                            "6. Admin         (cluster ops, users, audit)")
        choice = questionary.select(
            "Select an option:", choices=_choices, style=_STYLE
        ).ask()

        if choice is None or choice.startswith("5."):
            info("Goodbye.")
            return

        elif choice.startswith("1."):
            from iitgpu.wizard import run_wizard
            run_wizard()

        elif choice.startswith("2."):
            from iitgpu.workspace import run_workspace
            run_workspace()

        elif choice.startswith("3."):
            _jobs_menu()

        elif choice.startswith("4."):
            _settings_menu()

        elif choice.startswith("6."):
            from iitgpu.admin import admin_menu
            admin_menu()


def _jobs_menu() -> None:
    from iitgpu.dashboard import run_dashboard, run_hardware_stats
    from iitgpu.monitor import (show_queue, manage_job, browse_and_tail_log,
                                show_history, rerun_job)

    while True:
        header("Jobs")
        choice = questionary.select(
            "Jobs options:",
            choices=[
                "Live dashboard  (auto-refresh)",
                "View queue",
                "Manage a job  (cancel/hold/release/requeue/details)",
                "View job log",
                "Job history  (filters)",
                "Rerun a job",
                questionary.Separator("─────────────────────"),
                "Hardware stats",
                "Usage & accounting",
                "My running services",
                "Cluster status",
                "Back to main menu",
            ],
            style=_STYLE,
        ).ask()

        if choice is None or choice == "Back to main menu":
            return
        elif "Live dashboard" in choice:
            run_dashboard()
        elif choice == "View queue":
            show_queue()
        elif choice.startswith("Manage a job"):
            manage_job()
        elif choice == "View job log":
            browse_and_tail_log()
        elif choice.startswith("Job history"):
            show_history()
        elif choice == "Rerun a job":
            rerun_job()
        elif choice == "Hardware stats":
            run_hardware_stats()
        elif choice == "Usage & accounting":
            from iitgpu.accounting import usage_menu
            usage_menu()
        elif choice == "My running services":
            from iitgpu.notebooks import services_menu
            services_menu()
        elif choice == "Cluster status":
            _show_cluster_status()


def _settings_menu() -> None:
    from iitgpu.config import load_config as _lc, is_admin as _ia
    _cfg = _lc()
    _admin = _ia(_cfg)

    while True:
        header("Settings")
        _choices = [
            "Cluster health check",
            "Build environment",
            "Install prebuilt environment",
            "Run smoke test",
            "Advanced SLURM shell",
            "Cluster status",
            "Hardware stats (live)",
        ]
        if _admin:
            _choices.append("Admin panel")
        _choices.append("Back to main menu")

        choice = questionary.select(
            "Settings options:", choices=_choices, style=_STYLE
        ).ask()

        if choice is None or choice == "Back to main menu":
            return
        elif choice == "Cluster health check":
            from iitgpu.setup import check_cluster_health
            from iitgpu.ui import console
            result = check_cluster_health(_cfg)
            console.print(result)
        elif choice == "Build environment":
            from iitgpu.setup import _run_env_setup
            _run_env_setup(_cfg)
        elif choice == "Install prebuilt environment":
            from iitgpu.setup import _run_install_prebuilt
            _run_install_prebuilt(_cfg)
        elif choice == "Run smoke test":
            from iitgpu.setup import _run_smoke_test
            _run_smoke_test(_cfg)
        elif choice == "Advanced SLURM shell":
            from iitgpu.shell import run_shell
            run_shell()
        elif choice == "Cluster status":
            _show_cluster_status()
        elif choice == "Hardware stats (live)":
            from iitgpu.dashboard import run_hardware_stats
            run_hardware_stats()
        elif choice == "Admin panel":
            from iitgpu.admin import admin_menu
            admin_menu()


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
        table.add_row(
            p.name, f"[{s}]{p.state}[/]", str(p.nodes), str(p.gpus_per_node)
        )
    console.print(table)

