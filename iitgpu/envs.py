from __future__ import annotations

try:
    import fcntl as _fcntl
    def _flock(fh, op): _fcntl.flock(fh, op)
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_UN = _fcntl.LOCK_UN
except ImportError:
    def _flock(fh, op): pass  # type: ignore[misc]
    LOCK_EX = LOCK_UN = 0

import getpass
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import Config, models_dir
from iitgpu.ui import err, header, info, ok, warn

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
])


@dataclass
class EnvEntry:
    name: str
    kind: str    # "conda" | "venv"
    path: str


def _envs_registry_path(cfg: Config) -> Path:
    return Path(models_dir(cfg)) / ".envs.json"


def _load_venv_registry(cfg: Config) -> list[EnvEntry]:
    rpath = _envs_registry_path(cfg)
    if not rpath.exists():
        return []
    try:
        data = json.loads(rpath.read_text())
        return [EnvEntry(**e) for e in data if e.get("kind") == "venv"]
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


def _save_venv_registry(cfg: Config, entries: list[EnvEntry]) -> None:
    rpath = _envs_registry_path(cfg)
    rpath.parent.mkdir(parents=True, exist_ok=True)
    with open(rpath, "a+") as fh:
        try:
            _flock(fh, LOCK_EX)
            fh.seek(0)
            fh.truncate()
            json.dump([asdict(e) for e in entries], fh, indent=2)
        finally:
            _flock(fh, LOCK_UN)


def discover_conda_envs() -> list[EnvEntry]:
    try:
        result = subprocess.run(
            ["conda", "env", "list", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout)
        envs = []
        for path in data.get("envs", []):
            name = Path(path).name
            envs.append(EnvEntry(name=name, kind="conda", path=path))
        return envs
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return []


def list_all_envs(cfg: Config) -> list[EnvEntry]:
    conda = discover_conda_envs()
    venvs = _load_venv_registry(cfg)
    seen = {e.name for e in conda}
    return conda + [e for e in venvs if e.name not in seen]


def register_venv(cfg: Config, name: str, path: str) -> None:
    venvs = _load_venv_registry(cfg)
    venvs = [e for e in venvs if e.name != name]
    venvs.append(EnvEntry(name=name, kind="venv", path=path))
    _save_venv_registry(cfg, venvs)
    auditclient.log("env_register", detail=f"venv:{name}")


def _list_envs(cfg: Config) -> None:
    header("Environments")
    envs = list_all_envs(cfg)
    if not envs:
        info("No environments found.")
        return
    from rich.table import Table
    from iitgpu.ui import console
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Name", style="magenta")
    table.add_column("Type")
    table.add_column("Path", style="dim")
    for e in envs:
        table.add_row(e.name, e.kind, e.path)
    console.print(table)


def _register_venv_interactive(cfg: Config) -> None:
    header("Register venv")
    name = questionary.text("Environment name:", style=_STYLE).ask()
    if not name or not name.strip():
        return
    path = questionary.text(
        "Path to venv directory (must contain bin/activate):", style=_STYLE
    ).ask()
    if not path or not path.strip():
        return
    activate = Path(path.strip()) / "bin" / "activate"
    if not activate.exists():
        warn(f"No bin/activate found at {path} — registering anyway.")
    register_venv(cfg, name.strip(), path.strip())
    ok(f"Registered venv '{name.strip()}'.")


def env_menu(cfg: Config) -> None:
    while True:
        header("Environments")
        choice = questionary.select(
            "Environment options:",
            choices=["List environments", "Register venv", "Back to main menu"],
            style=_STYLE,
        ).ask()
        if choice is None or choice == "Back to main menu":
            return
        if choice == "List environments":
            _list_envs(cfg)
        elif choice == "Register venv":
            _register_venv_interactive(cfg)


def pick_env(cfg: Config) -> EnvEntry | None:
    """Return an environment the user selects, or None to skip."""
    envs = list_all_envs(cfg)
    if not envs:
        info("No environments found. Register a conda/venv via the Environments menu.")
        return None
    choices = [f"{e.name}  ({e.kind})" for e in envs] + ["[none / skip]"]
    choice = questionary.select("Pick an environment:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[none / skip]":
        return None
    name = choice.split("  ")[0]
    return next((e for e in envs if e.name == name), None)
