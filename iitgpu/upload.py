import os
import re
import getpass
import subprocess
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import load_config, make_shared_writable
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
        make_shared_writable(path)
        return os.access(path, os.W_OK)
    except OSError:
        return False


def _show_scp_instructions(folder_path: str, cfg) -> None:
    user = getpass.getuser()
    host = cfg.gateway_host
    port = cfg.gateway_port
    header("Upload via SCP / rsync")
    console.print(
        "\nOpen a [bold]new terminal on your local machine[/] and run one of these.\n"
        "[dim]Replace the example path with your actual data folder.[/]\n"
    )

    console.print("[bold]Linux / macOS[/]")
    console.print(
        f"  [bold cyan]scp[/]   -P {port} -r  \"/path/to/your-data\"  "
        f"[cyan]{user}@{host}:\"{folder_path}/\"[/]"
    )
    console.print(
        f"  [bold cyan]rsync[/] -avz --progress -e \"ssh -p {port}\"  "
        f"\"/path/to/your-data/\"  [cyan]{user}@{host}:\"{folder_path}/\"[/]"
    )
    console.print()

    console.print("[bold]Windows  (PowerShell / CMD)[/]")
    console.print(
        f"  [bold cyan]scp[/]   -P {port} -r  \"C:\\Users\\You\\your-data\"  "
        f"[cyan]{user}@{host}:\"{folder_path}/\"[/]"
    )
    console.print()

    console.print(
        f"[dim]Your local folder becomes a sub-folder here:[/]  "
        f"[cyan]{folder_path}[bold]/<your-data>/[/][/]"
    )
    console.print(
        "[dim]Reference it in your job script as:[/]  "
        f"[cyan]--data \"{folder_path}/<your-data>\"[/]\n"
    )
    console.print(
        "[dim]Note: paths with spaces or special characters ( ' [ ] ) must be\n"
        "      quoted exactly as shown above.  Do not add extra quotes around\n"
        "      the remote path on Windows — the outer \" \" are sufficient.[/]\n"
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
    import getpass
    from iitgpu.validate import in_user_upload_jail, user_upload_root

    cfg = load_config()
    user = getpass.getuser()

    # Uploads always land in the user's own isolated folder
    # (shared/users/<username>) — admins included. "Upload my data" belongs in
    # the personal folder; starting at the shared root exposed every folder and
    # let non-writable parents like shared/users be selected, which then failed
    # with "could not create or access". Admins who genuinely need to place
    # files elsewhere in /shared can do so from the file manager (Browse my
    # files), which retains full-jail access.
    upload_root = Path(user_upload_root(cfg.nfs_root, user))
    upload_root.mkdir(parents=True, exist_ok=True)
    base = upload_root
    _jail_check = lambda p: in_user_upload_jail(p, cfg.nfs_root, user)
    new_folder_label = f"New sub-folder name  (inside {upload_root}):"
    try:
        _existing = sorted(
            p.name for p in base.iterdir()
            if p.is_dir() and _jail_check(str(p))
        ) if base.exists() else []
    except OSError:
        _existing = []
    _folder_choices = (
        [questionary.Choice(f"{n}  ({base / n})", str(base / n)) for n in _existing]
        + [questionary.Choice("[upload here — my data folder]", str(base)),
           questionary.Choice("[create new sub-folder]",        "__new__"),
           questionary.Choice("[cancel]",                        "__cancel__")]
    )
    prompt = f"Select a destination inside your folder ({base}):"

    sel = questionary.select(prompt, choices=_folder_choices, style=_STYLE).ask()

    if sel is None or sel == "__cancel__":
        return

    if sel == "__new__":
        folder_name = questionary.text(
            new_folder_label,
            validate=lambda x: (
                _validate_folder_name(x.strip())
                or "Letters, digits, hyphens, underscores only — start with a letter or digit"
            ),
            style=_STYLE,
        ).ask()
        if not folder_name:
            return
        folder_name = folder_name.strip()
        folder_path = str(base / folder_name)
    else:
        folder_path = sel

    if not _jail_check(folder_path):
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
            _show_scp_instructions(folder_path, cfg)
        elif action == "url":
            _download_from_url(folder_path)
        elif action == "browse":
            _browse_folder(folder_path)
