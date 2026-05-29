import time
from rich.align import Align
from rich.panel import Panel
from rich.text import Text
from rich.console import Group
from rich import box
from iitgpu import __version__
from iitgpu.ui import console


def show_splash(pause: float = 1.5) -> None:
    heading = Text("IIT-GPU-Manager", style="bold bright_cyan", justify="center")

    tagline = Text(
        "GPU Cluster Job Manager  ·  SLURM 25.11.2  ·  RTX 5090",
        style="dim white",
        justify="center",
    )

    sep = Text("─" * 44, style="dim cyan", justify="center")

    footer = Text(justify="center")
    footer.append(f"v{__version__}", style="bold cyan")
    footer.append("   ·   ", style="dim white")
    footer.append("By: IIT Research Team", style="italic white")

    content = Group(
        Text(""),
        Align.center(heading),
        Text(""),
        Align.center(tagline),
        Text(""),
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
