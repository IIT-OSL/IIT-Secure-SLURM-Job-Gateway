# iitgpu/envbuilder.py
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from iitgpu.config import Config
from iitgpu.ui import console, err, info, ok, warn

# pip install args per framework key.
# PyTorch 2.5 uses cu124 (CUDA 12.4) — the highest stable build available on
# the PyTorch whl index. cu124 wheels run on CUDA 13.x drivers via backward
# compatibility, so they work on RTX 5090 nodes.
FRAMEWORK_PACKAGES: dict[str, list[str]] = {
    "pytorch-2.5": [
        "torch==2.5.* torchvision torchaudio "
        "--index-url https://download.pytorch.org/whl/cu124"
    ],
    "pytorch-2.4": [
        "torch==2.4.* torchvision torchaudio "
        "--index-url https://download.pytorch.org/whl/cu121"
    ],
    "tensorflow-2.18": ["tensorflow[and-cuda]==2.18.*"],
    "jax-0.4": ["jax[cuda12]"],
    "bare": [],
}

# Human-readable labels shown in the picker
FRAMEWORK_LABELS: dict[str, str] = {
    "pytorch-2.5":     "PyTorch 2.5  (CUDA 12.4 — RTX 5090 compatible)",
    "pytorch-2.4":     "PyTorch 2.4  (CUDA 12.1)",
    "tensorflow-2.18": "TensorFlow 2.18  (CUDA 12)",
    "jax-0.4":         "JAX 0.4  (CUDA 12)",
    "bare":            "Bare Python 3.11  (no ML framework)",
}

# Phase markers parsed from conda stdout; order matters.
_CONDA_PHASES: list[tuple[str, str]] = [
    ("Collecting package metadata", "Collecting metadata"),
    ("Solving environment",         "Solving environment"),
    ("Downloading and Extracting",  "Downloading packages"),
    ("Preparing transaction",       "Preparing transaction"),
    ("Verifying transaction",       "Verifying transaction"),
    ("Executing transaction",       "Executing transaction"),
]

# Phase markers parsed from pip stdout; order matters.
_PIP_PHASES: list[tuple[str, str]] = [
    ("Collecting",            "Collecting packages"),
    ("Downloading",           "Downloading packages"),
    ("Installing collected",  "Installing packages"),
    ("Successfully installed","Complete"),
]

# Ordered list of candidate conda binary paths searched when conda is not in PATH.
_CONDA_FALLBACK_PATHS = [
    "/shared/miniforge3/bin/conda",
    "/shared/public/miniforge3/bin/conda",
    "/opt/miniforge3/bin/conda",
    str(Path.home() / "miniforge3" / "bin" / "conda"),
    str(Path.home() / "miniconda3" / "bin" / "conda"),
    str(Path.home() / "anaconda3" / "bin" / "conda"),
]


def _find_conda(cfg: Config) -> str | None:
    """Return the path to the conda binary, or None if not found."""
    config_bin = str(Path(cfg.conda_prefix) / "bin" / "conda")
    if Path(config_bin).is_file():
        return config_bin
    found = shutil.which("conda")
    if found:
        return found
    for candidate in _CONDA_FALLBACK_PATHS:
        if Path(candidate).is_file():
            return candidate
    return None


def _envs_root(cfg: Config) -> Path:
    return Path(cfg.nfs_root) / "envs"


def _run_with_progress(
    cmd: list[str],
    phases: list[tuple[str, str]],
    label: str,
    env: dict | None = None,
) -> tuple[int, list[str]]:
    """Run *cmd* showing a Rich progress bar driven by output phase markers.

    stderr is merged into stdout so it is captured in the returned lines and
    shown by the progress display. Carriage-return animation characters emitted
    by conda's spinner are stripped before matching.

    Returns (returncode, output_lines).
    """
    output_lines: list[str] = []
    phase_idx = -1
    n = len(phases)
    first_label = phases[0][1] if phases else label

    with Progress(
        SpinnerColumn(),
        BarColumn(bar_width=40, complete_style="green", finished_style="bold green"),
        TextColumn("[bold cyan]{task.description}"),
        TextColumn("[dim]{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
        transient=False,
    ) as prog:
        task = prog.add_task(first_label, total=n)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # merge so errors are captured
            text=True,
            env=env,
            bufsize=1,
        )

        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.replace("\r", "").strip()
            if line:
                output_lines.append(line)

            # Advance through whichever phases this line signals.
            while phase_idx + 1 < n:
                marker, display = phases[phase_idx + 1]
                if marker.lower() in line.lower():
                    prog.advance(task, 1)
                    prog.update(task, description=display)
                    phase_idx += 1
                else:
                    break

        proc.wait()

        # Fill remaining steps so the bar always reaches 100 %.
        remaining = n - (phase_idx + 1)
        if remaining > 0:
            prog.advance(task, remaining)

    return proc.returncode, output_lines


def _show_error_lines(lines: list[str]) -> None:
    """Print lines that look like errors, up to the last 10."""
    relevant = [l for l in lines if "error" in l.lower() and l.strip()]
    for line in relevant[-10:]:
        console.print(f"  [bold red]{line}[/]")


def build_env(
    name: str,
    framework_key: str,
    requirements_path: str | None,
    cfg: Config,
) -> tuple[bool, str]:
    """Create a conda env at /shared/envs/{name} for the given framework.

    Returns (success, env_path).
    """
    conda_bin = _find_conda(cfg)
    if conda_bin is None:
        err("conda not found.")
        err(f"Expected Miniforge at: {cfg.conda_prefix}")
        info("Install with:")
        info("  wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh")
        info(f"  bash Miniforge3-Linux-x86_64.sh -b -p {cfg.conda_prefix}")
        return False, ""

    if framework_key not in FRAMEWORK_PACKAGES:
        err(f"Unknown framework: {framework_key}")
        return False, ""

    # Ensure conda bin is in PATH so pip/python inside the env resolve correctly.
    conda_bin_dir = str(Path(conda_bin).parent)
    env = {**os.environ, "PATH": f"{conda_bin_dir}:{os.environ.get('PATH', '')}"}

    envs_root = _envs_root(cfg)
    try:
        envs_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        err(f"Cannot create envs directory {envs_root}: {exc}")
        return False, ""

    env_path = str(envs_root / name)
    pip_path = str(Path(env_path) / "bin" / "pip")

    # ── Step 1: conda create ─────────────────────────────────────────────────
    info(f"Creating conda env at {env_path} ...")
    rc, lines = _run_with_progress(
        [conda_bin, "create", "-p", env_path, "python=3.11", "-y"],
        _CONDA_PHASES,
        "Creating conda environment",
        env=env,
    )
    if rc != 0:
        err("conda create failed.")
        _show_error_lines(lines)
        return False, ""

    # ── Step 2: install framework packages ───────────────────────────────────
    packages = FRAMEWORK_PACKAGES[framework_key]
    if packages:
        info(f"Installing {framework_key} packages ...")
        pkg_args: list[str] = []
        for pkg in packages:
            pkg_args.extend(pkg.split())

        rc, lines = _run_with_progress(
            [pip_path, "install"] + pkg_args,
            _PIP_PHASES,
            "Installing framework packages",
        )
        if rc != 0:
            err("pip install failed for framework packages.")
            _show_error_lines(lines)
            info(f"Index used: {next((a for a in pkg_args if a.startswith('http')), 'default')}")
            info("If the index has no matching wheel, try a different CUDA build or use 'bare' and install manually.")
            return False, ""

    # ── Step 3: requirements.txt ─────────────────────────────────────────────
    if requirements_path:
        info(f"Installing requirements from {requirements_path} ...")
        rc, lines = _run_with_progress(
            [pip_path, "install", "-r", requirements_path],
            _PIP_PHASES,
            "Installing requirements",
        )
        if rc != 0:
            warn("requirements.txt install had errors — env created but may be incomplete.")
            _show_error_lines(lines)

    ok(f"Environment '{name}' ready at {env_path}")
    return True, env_path
