# iitgpu/files.py
"""Jailed file operations + manager (Phase 5). Every path is validated with
in_jail() before any mutation. Promotes the upload.py feature set into a full
file manager (browse / mkdir / rename / delete / copy / disk usage)."""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from iitgpu.validate import in_jail, safe_listdir
from iitgpu import auditclient


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int


def list_dir(path: str) -> list[Entry]:
    """Jailed directory listing with sizes (dirs first, then files, sorted)."""
    if not in_jail(path):
        return []
    out: list[Entry] = []
    for name in safe_listdir(path):
        full = Path(path) / name
        try:
            st = full.stat()
            out.append(Entry(name, full.is_dir(), 0 if full.is_dir() else st.st_size))
        except OSError:
            continue
    return sorted(out, key=lambda e: (not e.is_dir, e.name.lower()))


def make_dir(parent: str, name: str) -> tuple[bool, str]:
    if not _valid_name(name):
        return False, "Invalid name (letters, digits, . _ - only)."
    target = str(Path(parent) / name)
    if not in_jail(target):
        return False, "Access denied: outside allowed directories."
    try:
        Path(target).mkdir(parents=True, exist_ok=False)
        auditclient.log("file_mkdir", detail=target,
                        meta={"path": target})
        return True, target
    except FileExistsError:
        return False, "Already exists."
    except OSError as exc:
        return False, str(exc)


def delete_path(path: str) -> tuple[bool, str]:
    if not in_jail(path):
        return False, "Access denied: outside allowed directories."
    p = Path(path)
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
        auditclient.log("file_delete", detail=str(p),
                        meta={"path": str(p)})
        return True, f"Deleted {p.name}"
    except OSError as exc:
        return False, str(exc)


def rename_path(path: str, new_name: str) -> tuple[bool, str]:
    if not _valid_name(new_name):
        return False, "Invalid name."
    if not in_jail(path):
        return False, "Access denied (source)."
    dest = str(Path(path).parent / new_name)
    if not in_jail(dest):
        return False, "Access denied (destination)."
    try:
        Path(path).rename(dest)
        auditclient.log("file_rename", detail=path,
                        meta={"src": path, "dest": dest})
        return True, dest
    except OSError as exc:
        return False, str(exc)


def copy_path(src: str, dest_dir: str) -> tuple[bool, str]:
    if not in_jail(src) or not in_jail(dest_dir):
        return False, "Access denied."
    dest = str(Path(dest_dir) / Path(src).name)
    if not in_jail(dest):
        return False, "Access denied (destination)."
    try:
        if Path(src).is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        auditclient.log("file_copy", detail=src,
                        meta={"src": src, "dest": dest})
        return True, dest
    except OSError as exc:
        return False, str(exc)


def disk_usage(path: str) -> tuple[int, int, int]:
    """Return (total, used, free) bytes for the filesystem holding path.
    (0,0,0) if path is outside the jail or unavailable."""
    if not in_jail(path):
        return (0, 0, 0)
    try:
        u = shutil.disk_usage(path)
        return (u.total, u.used, u.free)
    except OSError:
        return (0, 0, 0)


def dir_size(path: str) -> int:
    """Recursive byte size of a directory (jailed)."""
    if not in_jail(path):
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.1f} TB"


def _valid_name(name: str) -> bool:
    import re
    return bool(re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", name or ""))


def file_manager() -> None:
    """Interactive jailed file manager: browse, mkdir, rename, delete, copy,
    and see disk usage. Stays inside NFS_ROOT / the user area at all times."""
    import getpass
    import questionary
    from questionary import Style
    from iitgpu.config import load_config
    from iitgpu.ui import console, header, info, ok, err

    style = Style([("qmark", "fg:cyan bold"), ("pointer", "fg:cyan bold")])
    cfg = load_config()
    start = str(Path(cfg.nfs_root) / getpass.getuser())
    cur = start if Path(start).exists() and in_jail(start) else cfg.nfs_root

    while True:
        if not in_jail(cur):
            cur = cfg.nfs_root
        total, used, free = disk_usage(cur)
        header(f"Files — {cur}")
        if total:
            info(f"Disk: {fmt_size(free)} free of {fmt_size(total)}")
        entries = list_dir(cur)
        rows = ["[.. up]"] + [
            (f"[dir] {e.name}" if e.is_dir else f"      {e.name}  ({fmt_size(e.size)})")
            for e in entries
        ] + ["[ + new folder ]", "[ refresh ]", "[ back ]"]
        choice = questionary.select(f"{cur}", choices=rows, style=style).ask()
        if choice is None or choice == "[ back ]":
            return
        if choice == "[ refresh ]":
            continue
        if choice == "[.. up]":
            parent = str(Path(cur).parent)
            cur = parent if in_jail(parent) else cur
            continue
        if choice == "[ + new folder ]":
            name = questionary.text("New folder name:", style=style).ask()
            if name:
                good, msg = make_dir(cur, name.strip())
                (ok if good else err)(msg if not good else f"Created {name}")
            continue
        # selected an entry
        name = choice.replace("[dir] ", "").strip().split("  (")[0].strip()
        target = str(Path(cur) / name)
        if Path(target).is_dir() and choice.startswith("[dir]"):
            nav = questionary.select(
                f"{name}/", choices=["Open", "Rename", "Delete", "Cancel"], style=style
            ).ask()
            if nav == "Open" and in_jail(target):
                cur = target
            elif nav == "Rename":
                nn = questionary.text("New name:", default=name, style=style).ask()
                if nn:
                    good, msg = rename_path(target, nn.strip()); (ok if good else err)(str(msg))
            elif nav == "Delete":
                if questionary.confirm(f"Delete folder {name} and contents?", default=False, style=style).ask():
                    good, msg = delete_path(target); (ok if good else err)(str(msg))
            continue
        # file actions
        act = questionary.select(
            name, choices=["Rename", "Delete", "Show size", "Cancel"], style=style
        ).ask()
        if act == "Rename":
            nn = questionary.text("New name:", default=name, style=style).ask()
            if nn:
                good, msg = rename_path(target, nn.strip()); (ok if good else err)(str(msg))
        elif act == "Delete":
            if questionary.confirm(f"Delete {name}?", default=False, style=style).ask():
                good, msg = delete_path(target); (ok if good else err)(str(msg))
        elif act == "Show size":
            info(f"{name}: {fmt_size(Path(target).stat().st_size)}")
