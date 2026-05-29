import time
from rich.align import Align
from rich.panel import Panel
from rich.text import Text
from rich.console import Group
from rich import box
from iitgpu import __version__
from iitgpu.ui import console

_ART_IIT_GPU = r"""
  ___ ___ _____      ____ ____  _   _
 |_ _|_ _|_   _|    / ___|  _ \| | | |
  | | | |  | |     | |  _| |_) | | | |
  | | | |  | |     | |_| |  __/| |_| |
 |___|___| |_|      \____|_|    \___/
"""

_ART_MANAGER = r"""
  __  __
 |  \/  | __ _ _ __   __ _  __ _  ___ _ __
 | |\/| |/ _` | '_ \ / _` |/ _` |/ _ \ '__|
 | |  | | (_| | | | | (_| | (_| |  __/ |
 |_|  |_|\__,_|_| |_|\__,_|\__, |\___|_|
                             |___/
"""


def show_splash(pause: float = 1.5) -> None:
    iit_gpu = Text(_ART_IIT_GPU.strip("\n"), style="bold bright_cyan")
    manager = Text(_ART_MANAGER.strip("\n"), style="cyan")

    tagline = Text(
        "GPU Cluster Job Manager  ·  SLURM 25.11.2  ·  RTX 5090",
        style="dim white",
    )
    sep = Text("─" * 46, style="dim cyan")

    footer = Text()
    footer.append(f"v{__version__}", style="bold cyan")
    footer.append("   ·   ", style="dim white")
    footer.append("By: IIT Research Team", style="italic white")

    content = Group(
        Text(""),
        Align.center(iit_gpu),
        Align.center(manager),
        Text(""),
        Align.center(tagline),
        Align.center(sep),
        Text(""),
        Align.center(footer),
        Text(""),
    )

    console.print(
        Panel(
            content,
            box=box.DOUBLE_EDGE,
            border_style="cyan",
            expand=True,
            padding=(0, 2),
        )
    )
    console.print()

    if pause > 0:
        time.sleep(pause)
