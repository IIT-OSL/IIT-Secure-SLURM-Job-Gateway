# iitgpu/splash.py
import time
from iitgpu import __version__
from iitgpu.ui import console

_ART = r"""
  ___ ___ _____      ____ ____  _   _     __  __
 |_ _|_ _|_   _|    / ___|  _ \| | | |   |  \/  | __ _ _ __   __ _  __ _  ___ _ __
  | | | |  | |     | |  _| |_) | | | |   | |\/| |/ _` | '_ \ / _` |/ _` |/ _ \ '__|
  | | | |  | |     | |_| |  __/| |_| |   | |  | | (_| | | | | (_| | (_| |  __/ |
 |___|___| |_|      \____|_|    \___/    |_|  |_|\__,_|_| |_|\__,_|\__, |\___|_|
                                                                      |___/
"""


def show_splash(pause: float = 1.0) -> None:
    console.print(f"[bold cyan]{_ART}[/]")
    console.print(
        "[bold magenta]  GPU Cluster Job Manager for SLURM[/]  "
        f"[cyan]v{__version__}[/]  [dim]IIT HPC[/]"
    )
    console.rule(style="cyan")
    if pause > 0:
        time.sleep(pause)
