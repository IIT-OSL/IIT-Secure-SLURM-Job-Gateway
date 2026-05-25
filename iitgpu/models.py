from __future__ import annotations

try:
    import fcntl as _fcntl
    def _flock(fh, op): _fcntl.flock(fh, op)
    LOCK_EX = _fcntl.LOCK_EX
    LOCK_UN = _fcntl.LOCK_UN
except ImportError:
    # Windows — no-op; file locking not available
    def _flock(fh, op): pass  # type: ignore[misc]
    LOCK_EX = LOCK_UN = 0

import getpass
import json
import os
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import Config, models_dir
from iitgpu.ui import err, header, info, kv, ok, warn

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
])


@dataclass
class ModelEntry:
    name: str
    source: str        # "huggingface" | "url"
    path: str
    added_at: str
    added_by: str
    size_mb: float


def _registry_path(cfg: Config) -> Path:
    return Path(models_dir(cfg)) / ".registry.json"


def load_registry(cfg: Config) -> list[ModelEntry]:
    rpath = _registry_path(cfg)
    if not rpath.exists():
        return []
    try:
        data = json.loads(rpath.read_text())
        return [ModelEntry(**e) for e in data]
    except (json.JSONDecodeError, TypeError, KeyError):
        return []


def _save_registry(cfg: Config, entries: list[ModelEntry]) -> None:
    rpath = _registry_path(cfg)
    rpath.parent.mkdir(parents=True, exist_ok=True)
    with open(rpath, "a+") as fh:
        try:
            _flock(fh, LOCK_EX)
            fh.seek(0)
            fh.truncate()
            json.dump([asdict(e) for e in entries], fh, indent=2)
        finally:
            _flock(fh, LOCK_UN)


def register_model(cfg: Config, entry: ModelEntry) -> None:
    entries = load_registry(cfg)
    entries = [e for e in entries if e.name != entry.name]
    entries.append(entry)
    _save_registry(cfg, entries)
    auditclient.log("model_register", detail=entry.name)


def remove_model(cfg: Config, name: str) -> bool:
    entries = load_registry(cfg)
    before = len(entries)
    entries = [e for e in entries if e.name != name]
    if len(entries) == before:
        return False
    _save_registry(cfg, entries)
    auditclient.log("model_delete", detail=name)
    return True


def _dir_size_mb(path: str) -> float:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return round(total / (1024 * 1024), 1)


def download_hf(cfg: Config, repo_id: str) -> tuple[bool, str]:
    """Download a HuggingFace model using huggingface_hub."""
    dest = str(Path(models_dir(cfg)) / repo_id.replace("/", "--"))
    auditclient.log("model_download_start", detail=f"hf:{repo_id}")
    try:
        from huggingface_hub import snapshot_download
        snapshot_download(repo_id=repo_id, local_dir=dest, local_dir_use_symlinks=False)
    except ImportError:
        err("huggingface_hub not installed. Run: pip install huggingface-hub")
        auditclient.log("model_download_failed", detail=f"hf:{repo_id} missing_dep")
        return False, "huggingface_hub not installed"
    except Exception as exc:
        auditclient.log("model_download_failed", detail=f"hf:{repo_id} {exc}")
        return False, str(exc)

    size = _dir_size_mb(dest)
    entry = ModelEntry(
        name=repo_id,
        source="huggingface",
        path=dest,
        added_at=datetime.now(timezone.utc).isoformat(),
        added_by=getpass.getuser(),
        size_mb=size,
    )
    register_model(cfg, entry)
    auditclient.log("model_download_ok", detail=f"hf:{repo_id} path={dest}")
    return True, dest


def download_url(cfg: Config, url: str, name: str) -> tuple[bool, str]:
    """Download a model file from an arbitrary URL."""
    safe_name = name.replace("/", "_").replace(" ", "_")
    dest_dir = Path(models_dir(cfg)) / safe_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = url.split("/")[-1].split("?")[0] or "model.bin"
    dest = str(dest_dir / filename)
    auditclient.log("model_download_start", detail=f"url:{url}")
    try:
        info(f"Downloading {url} ...")
        urllib.request.urlretrieve(url, dest)
    except Exception as exc:
        auditclient.log("model_download_failed", detail=f"url:{url} {exc}")
        return False, str(exc)

    size = _dir_size_mb(str(dest_dir))
    entry = ModelEntry(
        name=name,
        source="url",
        path=str(dest_dir),
        added_at=datetime.now(timezone.utc).isoformat(),
        added_by=getpass.getuser(),
        size_mb=size,
    )
    register_model(cfg, entry)
    auditclient.log("model_download_ok", detail=f"url:{url} path={dest}")
    return True, dest


def _list_models(cfg: Config) -> None:
    header("Model Library")
    entries = load_registry(cfg)
    if not entries:
        info("No models registered yet.")
        return
    from rich.table import Table
    from iitgpu.ui import console
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Name", style="magenta")
    table.add_column("Source")
    table.add_column("Size (MB)", justify="right")
    table.add_column("Added by")
    table.add_column("Path", style="dim")
    for e in entries:
        table.add_row(e.name, e.source, str(e.size_mb), e.added_by, e.path)
    console.print(table)


def _download_hf_interactive(cfg: Config) -> None:
    header("Download HuggingFace Model")
    repo = questionary.text(
        "HuggingFace repo ID (e.g. meta-llama/Llama-3-8B):",
        style=_STYLE,
    ).ask()
    if not repo or not repo.strip():
        return
    repo = repo.strip()
    ok_flag, result = download_hf(cfg, repo)
    if ok_flag:
        ok(f"Downloaded to: {result}")
    else:
        err(f"Download failed: {result}")


def _download_url_interactive(cfg: Config) -> None:
    header("Download Model from URL")
    url = questionary.text("URL:", style=_STYLE).ask()
    if not url or not url.strip():
        return
    name = questionary.text(
        "Short name for this model (used in registry):", style=_STYLE
    ).ask()
    if not name or not name.strip():
        return
    ok_flag, result = download_url(cfg, url.strip(), name.strip())
    if ok_flag:
        ok(f"Downloaded to: {result}")
    else:
        err(f"Download failed: {result}")


def _remove_interactive(cfg: Config) -> None:
    entries = load_registry(cfg)
    if not entries:
        info("No models to remove.")
        return
    choices = [f"{e.name}  ({e.source}, {e.size_mb} MB)" for e in entries] + ["[back]"]
    choice = questionary.select("Select model to remove from registry:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[back]":
        return
    name = choice.split("  ")[0]
    if not questionary.confirm(f"Remove '{name}' from registry? (files are NOT deleted)", default=False, style=_STYLE).ask():
        return
    if remove_model(cfg, name):
        ok(f"Removed '{name}' from registry.")
    else:
        warn("Model not found.")


def model_menu(cfg: Config) -> None:
    while True:
        header("Model Library")
        choice = questionary.select(
            "Model options:",
            choices=[
                "List models",
                "Download from HuggingFace Hub",
                "Download from URL",
                "Remove from registry",
                "Back to main menu",
            ],
            style=_STYLE,
        ).ask()
        if choice is None or choice == "Back to main menu":
            return
        if choice == "List models":
            _list_models(cfg)
        elif choice == "Download from HuggingFace Hub":
            _download_hf_interactive(cfg)
        elif choice == "Download from URL":
            _download_url_interactive(cfg)
        elif choice == "Remove from registry":
            _remove_interactive(cfg)


def pick_model(cfg: Config) -> ModelEntry | None:
    """Return a model the user picks, or None if they skip."""
    entries = load_registry(cfg)
    if not entries:
        info("No models in library. Download one from the Model Library menu first.")
        return None
    choices = [f"{e.name}  ({e.source}, {e.size_mb} MB)" for e in entries] + ["[none / skip]"]
    choice = questionary.select("Pick a model:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[none / skip]":
        return None
    name = choice.split("  ")[0]
    return next((e for e in entries if e.name == name), None)
