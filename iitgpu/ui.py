# iitgpu/ui.py
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel

_theme = Theme({
    "info": "cyan",
    "ok": "bold green",
    "warn": "bold yellow",
    "err": "bold red",
    "label": "bold magenta",
    "value": "white",
})

console = Console(theme=_theme)


def header(text: str) -> None:
    console.rule(f"[bold cyan]{text}[/]")


def ok(text: str) -> None:
    console.print(f"[ok]✔  {text}[/]")


def warn(text: str) -> None:
    console.print(f"[warn]⚠  {text}[/]")


def err(text: str) -> None:
    console.print(f"[err]✘  {text}[/]")


def info(text: str) -> None:
    console.print(f"[info]{text}[/]")


def kv(key: str, value: str) -> None:
    console.print(f"[label]{key}:[/] [value]{value}[/]")


def panel(title: str, body: str) -> None:
    console.print(Panel(body, title=title, border_style="cyan"))
