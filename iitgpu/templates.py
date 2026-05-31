from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import questionary
from questionary import Style

from iitgpu import auditclient
from iitgpu.config import Config, templates_dir
from iitgpu.jobs import JobSpec
from iitgpu.ui import err, header, info, ok, warn

_STYLE = Style([
    ("qmark", "fg:cyan bold"),
    ("question", "bold"),
    ("answer", "fg:magenta bold"),
    ("pointer", "fg:cyan bold"),
    ("highlighted", "fg:cyan bold"),
])

# Built-in presets — never written to disk, read-only.
_BUILTIN_PRESETS: dict[str, dict] = {
    "PyTorch Training": {
        "job_name": "pytorch_train",
        "partition": "gpu",
        "gpus": 1,
        "cpus": 16,
        "mem_gb": 60,
        "time_limit": "",
        "run_command": "python train.py",
        "modules": [],
        "uploads": [],
        "model_path": "",
        "conda_env": "",
        "venv_path": "",
        "task_type": "train",
    },
    "HuggingFace Fine-tune": {
        "job_name": "hf_finetune",
        "partition": "gpu",
        "gpus": 1,
        "cpus": 16,
        "mem_gb": 60,
        "time_limit": "",
        "run_command": "python finetune.py --model $MODEL_PATH",
        "modules": [],
        "uploads": [],
        "model_path": "",
        "conda_env": "",
        "venv_path": "",
        "task_type": "finetune",
    },
    "Inference / Serving": {
        "job_name": "inference",
        "partition": "gpu",
        "gpus": 1,
        "cpus": 8,
        "mem_gb": 32,
        "time_limit": "04:00:00",
        "run_command": "python serve.py --model $MODEL_PATH",
        "modules": [],
        "uploads": [],
        "model_path": "",
        "conda_env": "",
        "venv_path": "",
        "task_type": "inference",
    },
    "Quick Debug": {
        "job_name": "debug_run",
        "partition": "gpu",
        "gpus": 1,
        "cpus": 4,
        "mem_gb": 16,
        "time_limit": "00:30:00",
        "run_command": "python test_script.py",
        "modules": [],
        "uploads": [],
        "model_path": "",
        "conda_env": "",
        "venv_path": "",
        "task_type": "test",
    },
}


def _template_path(cfg: Config, name: str) -> Path:
    safe = name.replace("/", "_").replace(" ", "_")
    return Path(templates_dir(cfg)) / f"{safe}.json"


def list_templates(cfg: Config) -> dict[str, str]:
    """Returns {display_name: source} where source is 'builtin' or 'saved'."""
    result = {name: "builtin" for name in _BUILTIN_PRESETS}
    tdir = Path(templates_dir(cfg))
    if tdir.exists():
        for p in sorted(tdir.glob("*.json")):
            display = p.stem.replace("_", " ")
            if display not in result:
                result[display] = "saved"
    return result


def load_template(cfg: Config, name: str) -> dict | None:
    if name in _BUILTIN_PRESETS:
        return dict(_BUILTIN_PRESETS[name])
    tpath = _template_path(cfg, name)
    if not tpath.exists():
        # Try stem-based match
        tdir = Path(templates_dir(cfg))
        for p in tdir.glob("*.json"):
            if p.stem.replace("_", " ") == name:
                tpath = p
                break
        else:
            return None
    try:
        return json.loads(tpath.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_template(cfg: Config, name: str, spec: JobSpec) -> bool:
    tdir = Path(templates_dir(cfg))
    tdir.mkdir(parents=True, exist_ok=True)
    tpath = _template_path(cfg, name)
    try:
        data = asdict(spec)
        tpath.write_text(json.dumps(data, indent=2))
        make_shared_writable(tpath)
        auditclient.log("template_save", detail=name)
        return True
    except OSError as exc:
        err(f"Could not save template: {exc}")
        return False


def delete_template(cfg: Config, name: str) -> bool:
    if name in _BUILTIN_PRESETS:
        warn("Built-in presets cannot be deleted.")
        return False
    tpath = _template_path(cfg, name)
    if not tpath.exists():
        tdir = Path(templates_dir(cfg))
        for p in tdir.glob("*.json"):
            if p.stem.replace("_", " ") == name:
                tpath = p
                break
        else:
            return False
    try:
        tpath.unlink()
        auditclient.log("template_delete", detail=name)
        return True
    except OSError:
        return False


def _list_templates(cfg: Config) -> None:
    header("Templates")
    templates = list_templates(cfg)
    if not templates:
        info("No templates available.")
        return
    from rich.table import Table
    from iitgpu.ui import console
    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Name", style="magenta")
    table.add_column("Type")
    for name, source in templates.items():
        label = "[bold]built-in[/]" if source == "builtin" else "saved"
        table.add_row(name, label)
    console.print(table)


def _delete_interactive(cfg: Config) -> None:
    templates = list_templates(cfg)
    saved = [n for n, s in templates.items() if s == "saved"]
    if not saved:
        info("No saved templates to delete.")
        return
    choices = saved + ["[back]"]
    choice = questionary.select("Select template to delete:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[back]":
        return
    if questionary.confirm(f"Delete template '{choice}'?", default=False, style=_STYLE).ask():
        if delete_template(cfg, choice):
            ok(f"Deleted '{choice}'.")
        else:
            err("Could not delete template.")


def template_menu(cfg: Config) -> None:
    while True:
        header("Templates")
        choice = questionary.select(
            "Template options:",
            choices=["List templates", "Delete saved template", "Back to main menu"],
            style=_STYLE,
        ).ask()
        if choice is None or choice == "Back to main menu":
            return
        if choice == "List templates":
            _list_templates(cfg)
        elif choice == "Delete saved template":
            _delete_interactive(cfg)


def pick_template(cfg: Config) -> dict | None:
    """Return a template dict the user picks, or None to skip."""
    templates = list_templates(cfg)
    if not templates:
        info("No templates available.")
        return None
    choices = [
        f"{name}  [{'built-in' if src == 'builtin' else 'saved'}]"
        for name, src in templates.items()
    ] + ["[none / skip]"]
    choice = questionary.select("Load a template:", choices=choices, style=_STYLE).ask()
    if choice is None or choice == "[none / skip]":
        return None
    name = choice.split("  [")[0]
    data = load_template(cfg, name)
    if data is None:
        err(f"Could not load template '{name}'.")
        return None
    auditclient.log("template_load", detail=name)
    return data
