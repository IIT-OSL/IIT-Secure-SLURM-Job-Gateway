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
    header("Upload via SCP  (zip first — it's faster & more reliable)")

    console.print(
        "\n[bold]Step 1 — On your computer, split your work into two folders[/] "
        "and zip each one [bold]separately[/]:\n"
        "  • [cyan]dataset/[/]  — your data files (images, csv, etc.)\n"
        "  • [cyan]scripts/[/]  — your code (.py, requirements.txt, notebooks)\n"
        "[dim]Two small zips upload far faster and more reliably than thousands of\n"
        "loose files, and keep your data and code cleanly separated here.[/]\n"
    )

    console.print("[bold]Linux / macOS[/]  [dim](in the folder that holds dataset/ and scripts/)[/]")
    console.print("  [bold cyan]zip -r dataset.zip dataset/[/]")
    console.print("  [bold cyan]zip -r scripts.zip scripts/[/]")
    console.print()
    console.print("[bold]Windows  (PowerShell)[/]")
    console.print("  [bold cyan]Compress-Archive -Path dataset -DestinationPath dataset.zip[/]")
    console.print("  [bold cyan]Compress-Archive -Path scripts -DestinationPath scripts.zip[/]")
    console.print()

    console.print(
        "[bold]Step 2 — Upload the two .zip files[/] "
        "(run in a [bold]new terminal on your computer[/]):\n"
    )
    console.print("[bold]Linux / macOS[/]")
    console.print(
        f"  [bold cyan]scp[/]   -P {port}  dataset.zip scripts.zip  "
        f"[cyan]{user}@{host}:\"{folder_path}/\"[/]"
    )
    console.print(
        f"  [dim]big dataset? resumable:[/] [bold cyan]rsync[/] -avz --progress "
        f"-e \"ssh -p {port}\"  dataset.zip scripts.zip  "
        f"[cyan]{user}@{host}:\"{folder_path}/\"[/]"
    )
    console.print()
    console.print("[bold]Windows  (PowerShell / CMD)[/]")
    console.print(
        f"  [bold cyan]scp[/]   -P {port}  dataset.zip scripts.zip  "
        f"[cyan]{user}@{host}:\"{folder_path}/\"[/]"
    )
    console.print()

    console.print(
        "[bold]Step 3 — Back here, choose[/] [cyan]\"Unzip an uploaded .zip\"[/] "
        "to extract each archive\n          into its own folder (with a progress bar).\n"
    )
    console.print(
        "[dim]After unzipping, reference your files in a job script as:[/]  "
        f"[cyan]\"{folder_path}/dataset/...\"[/]\n",
        soft_wrap=True,
    )
    console.print(
        "[dim]Note: keep the outer \" \" quotes on the remote path exactly as shown.\n"
        "      Don't add extra quotes on Windows.[/]\n"
    )
    questionary.press_any_key_to_continue("Press any key when done").ask()


def _unzip_in_folder(folder_path: str, cfg) -> None:
    """Extract a user-uploaded .zip into its own sub-folder, with a live
    byte-progress bar. Stays strictly inside the user's upload jail and guards
    against zip-slip (archive members that try to escape the target dir)."""
    import zipfile
    from rich.progress import (
        BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
        TextColumn, TimeElapsedColumn,
    )
    from iitgpu.validate import in_user_upload_jail

    user = getpass.getuser()
    header("Unzip an uploaded .zip")
    try:
        zips = sorted(
            p for p in Path(folder_path).iterdir()
            if p.is_file() and p.suffix.lower() == ".zip"
        )
    except OSError as exc:
        err(str(exc)); return
    if not zips:
        info("No .zip files found in this folder.")
        console.print(
            "[dim]Upload your zipped dataset/scripts first (see the SCP "
            "instructions), then come back here to extract them.[/]"
        )
        questionary.press_any_key_to_continue("Press any key to continue").ask()
        return

    choices = [
        questionary.Choice(f"{z.name}   ({z.stat().st_size:,} bytes)", str(z))
        for z in zips
    ] + [questionary.Choice("[cancel]", "__cancel__")]
    sel = questionary.select(
        "Which archive do you want to unzip?", choices=choices, style=_STYLE
    ).ask()
    if not sel or sel == "__cancel__":
        return

    zip_path = Path(sel)
    dest = Path(folder_path) / zip_path.stem
    # The extraction target must stay inside the user's own upload folder.
    if not in_user_upload_jail(str(dest), cfg.nfs_root, user):
        err("Refusing to extract outside your folder."); return
    if dest.exists() and any(dest.iterdir()):
        if not questionary.confirm(
            f"'{dest.name}/' already exists and is not empty. Extract into it anyway?",
            default=False, style=_STYLE,
        ).ask():
            return
    dest.mkdir(parents=True, exist_ok=True)
    make_shared_writable(str(dest))
    dest_resolved = dest.resolve()

    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [m for m in zf.infolist() if not m.is_dir()]
            if not members:
                err("Archive is empty."); return
            # zip-slip guard: every member must resolve inside the target dir.
            for m in members:
                target = (dest / m.filename).resolve()
                if target != dest_resolved and not str(target).startswith(
                    str(dest_resolved) + os.sep
                ):
                    err(f"Archive contains an unsafe path ('{m.filename}') — "
                        "aborting for safety.")
                    return

            auditclient.log("data_unzip", detail=str(zip_path))
            total = sum(m.file_size for m in members) or 1
            with Progress(
                SpinnerColumn(),
                TextColumn("[bold cyan]{task.description}"),
                BarColumn(bar_width=30, complete_style="green"),
                TextColumn("[dim]{task.fields[fname]:<36}"),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console,
            ) as prog:
                task = prog.add_task("Unzipping", total=total, fname="")
                for m in members:
                    target = dest / m.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, open(target, "wb") as out:
                        while True:
                            chunk = src.read(256 * 1024)
                            if not chunk:
                                break
                            out.write(chunk)
                            prog.update(task, advance=len(chunk),
                                        fname=Path(m.filename).name[:36])
                prog.update(task, completed=total, fname="done")
    except zipfile.BadZipFile:
        err("That file is not a valid .zip archive."); return
    except OSError as exc:
        err(f"Extraction failed: {exc}"); return

    ok(f"Extracted {len(members)} file(s)  →  [cyan]{dest}[/]")
    console.print(
        f"[dim]Reference it in a job script as:[/]  [cyan]\"{dest}/...\"[/]"
    )
    questionary.press_any_key_to_continue("Press any key to continue").ask()


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
        questionary.Choice("Upload from my computer  (zip + SCP instructions)",    "scp"),
        questionary.Choice("Unzip an uploaded .zip  (extract here, with progress)", "unzip"),
        questionary.Choice("Download from a URL  (wget / curl on the server)",      "url"),
        questionary.Choice("Browse folder contents",                                "browse"),
        questionary.Choice("Back to main menu",                                     "back"),
    ]

    while True:
        action = questionary.select(
            "What would you like to do?", choices=choices, style=_STYLE
        ).ask()
        if action is None or action == "back":
            break
        elif action == "scp":
            _show_scp_instructions(folder_path, cfg)
        elif action == "unzip":
            _unzip_in_folder(folder_path, cfg)
        elif action == "url":
            _download_from_url(folder_path)
        elif action == "browse":
            _browse_folder(folder_path)
