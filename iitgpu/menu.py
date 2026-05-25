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
    "1. Create & submit GPU job",
    "2. Monitor jobs",
    "3. Cluster status",
    "4. Model library",
    "5. Environments",
    "6. Templates",
    "7. Settings (read-only)",
    "8. Quit",
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
    info("[dim]Settings are controlled by your admin. NFS_ROOT cannot be changed here.[/]")


def run_menu() -> None:
    from iitgpu.monitor import cluster_status, monitor_menu
    from iitgpu.wizard import run_wizard

    cfg = load_config()

    while True:
        header("Main Menu")
        choice = questionary.select("Select an option:", choices=_ITEMS, style=_STYLE).ask()
        if choice is None or choice.startswith("8."):
            info("Goodbye.")
            return
        elif choice.startswith("1."):
            run_wizard()
        elif choice.startswith("2."):
            monitor_menu()
        elif choice.startswith("3."):
            cluster_status()
        elif choice.startswith("4."):
            from iitgpu.models import model_menu
            model_menu(cfg)
        elif choice.startswith("5."):
            from iitgpu.envs import env_menu
            env_menu(cfg)
        elif choice.startswith("6."):
            from iitgpu.templates import template_menu
            template_menu(cfg)
        elif choice.startswith("7."):
            _show_settings()
