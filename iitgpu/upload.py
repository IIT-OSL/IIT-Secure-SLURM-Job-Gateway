import os
import re
import socket
import getpass
import subprocess
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import load_config
from iitgpu.ui import console, ok, err, info, header
from iitgpu.validate import in_jail

_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

_STYLE = Style([
    ("qmark",       "fg:cyan bold"),
    ("question",    "bold"),
    ("answer",      "fg:magenta bold"),
    ("pointer",     "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
])


def _validate_folder_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def _ensure_folder(path: str) -> bool:
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _show_scp_instructions(folder_path: str) -> None:
    hostname = socket.gethostname()
    user = getpass.getuser()
    header("Upload via SCP / rsync")
    console.print(
        "\nOpen a [bold]new terminal on your local machine[/] and run either of these:\n"
    )
    console.print(
        f"  [bold cyan]scp[/]   -r  /path/to/local/data/  "
        f"[cyan]{user}@{hostname}:{folder_path}/[/]"
    )
    console.print()
    console.print(
        f"  [bold cyan]rsync[/] -avz --progress  /path/to/local/data/  "
        f"[cyan]{user}@{hostname}:{folder_path}/[/]"
    )
    console.print()
    console.print(f"[dim]Data will be stored at:[/]  [cyan]{folder_path}[/]")
    console.print(
        "[dim]Reference this path in your job script as:[/]  "
        f"[cyan]--data {folder_path}[/]\n"
    )
    questionary.press_any_key_to_continue("Press any key when done").ask()


def _browse_folder(folder_path: str) -> None:
    header(f"Contents  —  {folder_path}")
    try:
        entries = sorted(Path(folder_path).iterdir())
    except OSError as exc:
        err(str(exc))
        return
    if not entries:
        info("[dim]Folder is empty.[/]")
    else:
        for entry in entries:
            if entry.is_dir():
                console.print(f"  [cyan]{entry.name}/[/]")
            else:
                try:
                    size = entry.stat().st_size
                    size_str = f"  [dim]{size:,} B[/]"
                except OSError:
                    size_str = ""
                console.print(f"  {entry.name}{size_str}")
    console.print()
    questionary.press_any_key_to_continue("").ask()


def _download_from_url(folder_path: str) -> None:
    header("Download from URL")
    url = questionary.text(
        "URL to download  (https:// or http://):",
        style=_STYLE,
    ).ask()
    if not url:
        return
    url = url.strip()
    if not url.startswith(("https://", "http://")):
        err("Only https:// and http:// URLs are supported.")
        return

    raw = url.rstrip("/").split("/")[-1].split("?")[0]
    filename = re.sub(r"[^A-Za-z0-9._-]", "_", raw) or "download"
    target = str(Path(folder_path) / filename)

    if not in_jail(target):
        err("Resolved path is outside the allowed directory.")
        return

    auditclient.log("data_download_url", detail=url)
    info(f"Saving to [cyan]{target}[/] …\n")

    for cmd in (
        ["wget", "--show-progress", "-q", "-O", target, url],
        ["curl", "-L", "--progress-bar", "-o", target, url],
    ):
        try:
            result = subprocess.run(cmd, check=False)
            if result.returncode == 0:
                size = Path(target).stat().st_size if Path(target).exists() else 0
                ok(f"Saved  {filename}  ({size:,} bytes)  →  {target}")
                return
            Path(target).unlink(missing_ok=True)
        except FileNotFoundError:
            continue

    err("Download failed — check the URL and try again.")


def run_upload() -> None:
    cfg = load_config()

    folder_name = questionary.text(
        "Folder name  (will be created at /shared/<name>):",
        validate=lambda x: (
            _validate_folder_name(x.strip())
            or "Letters, digits, hyphens, underscores only — start with a letter or digit"
        ),
        style=_STYLE,
    ).ask()
    if not folder_name:
        return

    folder_name = folder_name.strip()
    folder_path = str(Path(cfg.nfs_root) / folder_name)

    if not in_jail(folder_path):
        err("Folder path is outside the allowed directory.")
        return

    if not _ensure_folder(folder_path):
        err(f"Could not create or access {folder_path} — check permissions.")
        return

    ok(f"Data folder ready:  [cyan]{folder_path}[/]")
    auditclient.log("data_folder_open", detail=folder_path)

    choices = [
        questionary.Choice("Upload from my computer  (SCP / rsync instructions)", "scp"),
        questionary.Choice("Download from a URL  (wget / curl on the server)",    "url"),
        questionary.Choice("Browse folder contents",                               "browse"),
        questionary.Choice("Back to main menu",                                    "back"),
    ]

    while True:
        action = questionary.select(
            "What would you like to do?", choices=choices, style=_STYLE
        ).ask()
        if action is None or action == "back":
            break
        elif action == "scp":
            _show_scp_instructions(folder_path)
        elif action == "url":
            _download_from_url(folder_path)
        elif action == "browse":
            _browse_folder(folder_path)
