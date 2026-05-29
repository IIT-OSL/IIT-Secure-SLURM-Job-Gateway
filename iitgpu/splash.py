import time
from rich.align import Align
from rich.panel import Panel
from rich.text import Text
from rich.rule import Rule
from rich import box
from iitgpu import __version__
from iitgpu.ui import console

# "IIT GPU" — figlet block, ~40 cols
_ART_TOP = r"""
  ___ ___ _____      ____ ____  _   _
 |_ _|_ _|_   _|    / ___|  _ \| | | |
  | | | |  | |     | |  _| |_) | | | |
  | | | |  | |     | |_| |  __/| |_| |
 |___|___| |_|      \____|_|    \___/
"""

# "Manager" — figlet block, ~45 cols
_ART_BOT = r"""
  __  __
 |  \/  | __ _ _ __   __ _  __ _  ___ _ __
 | |\/| |/ _` | '_ \ / _` |/ _` |/ _ \ '__|
 | |  | | (_| | | | | (_| | (_| |  __/ |
 |_|  |_|\__,_|_| |_|\__,_|\__, |\___|_|
                             |___/
"""


def show_splash(pause: float = 1.5) -> None:
    console.print()
    console.print(Rule(style="cyan"))

    console.print(Text(_ART_TOP, style="bold bright_cyan", justify="center"))
    console.print(Text(_ART_BOT, style="cyan", justify="center"))

    console.print(Rule(style="dim cyan"))
    console.print()

    subtitle = Text(justify="center")
    subtitle.append("GPU Cluster Job Manager", style="dim white")
    subtitle.append("  ·  ", style="dim cyan")
    subtitle.append("SLURM 25.11.2", style="dim white")
    subtitle.append("  ·  ", style="dim cyan")
    subtitle.append("RTX 5090  /  IIT HPC Cluster", style="dim white")

    footer = Text(justify="center")
    footer.append(f"v{__version__}", style="bold cyan")
    footer.append("     ", style="dim white")
    footer.append("By: ", style="dim white")
    footer.append("IIT Research Team", style="italic white")

    credits = Panel(
        Align.center(
            Text.assemble(
                Text("\n"),
                subtitle,
                Text("\n\n"),
                footer,
                Text("\n"),
            )
        ),
        box=box.SIMPLE_HEAVY,
        border_style="dim cyan",
        expand=False,
        padding=(0, 6),
    )

    console.print(Align.center(credits))
    console.print()

    if pause > 0:
        time.sleep(pause)
