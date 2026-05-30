# iitgpu/envbuilder.py
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from iitgpu.config import Config
from iitgpu.ui import err, info, ok, warn

# pip install args per framework key.
FRAMEWORK_PACKAGES: dict[str, list[str]] = {
    "pytorch-2.5": [
        "torch==2.5.* torchvision torchaudio "
        "--index-url https://download.pytorch.org/whl/cu131"
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
    "pytorch-2.5":     "PyTorch 2.5  (CUDA 13.2 — recommended)",
    "pytorch-2.4":     "PyTorch 2.4  (CUDA 12.1)",
    "tensorflow-2.18": "TensorFlow 2.18  (CUDA 12)",
    "jax-0.4":         "JAX 0.4  (CUDA 12)",
    "bare":            "Bare Python 3.11  (no ML framework)",
}

# Ordered list of candidate conda binary paths searched when conda is not in PATH.
# The launcher strips PATH to /usr/local/bin:/usr/bin:/bin for security, so
# shutil.which("conda") always returns None even when Miniforge is installed.
_CONDA_FALLBACK_PATHS = [
    "/shared/miniforge3/bin/conda",
    "/shared/public/miniforge3/bin/conda",
    "/opt/miniforge3/bin/conda",
    str(Path.home() / "miniforge3" / "bin" / "conda"),
    str(Path.home() / "miniconda3" / "bin" / "conda"),
    str(Path.home() / "anaconda3" / "bin" / "conda"),
]


def _find_conda(cfg: Config) -> str | None:
    """Return the path to the conda binary, or None if not found.

    Search order:
      1. cfg.conda_prefix/bin/conda  (the install.sh-configured location)
      2. PATH (shutil.which)
      3. Known fallback paths
    """
    # 1. Config-specified prefix — most reliable since install.sh sets CONDA_PREFIX_SHARED
    config_bin = str(Path(cfg.conda_prefix) / "bin" / "conda")
    if Path(config_bin).is_file():
        return config_bin

    # 2. PATH — works in interactive shells; not in the locked-down launcher env
    found = shutil.which("conda")
    if found:
        return found

    # 3. Common installation paths
    for candidate in _CONDA_FALLBACK_PATHS:
        if Path(candidate).is_file():
            return candidate

    return None


def _envs_root(cfg: Config) -> Path:
    return Path(cfg.nfs_root) / "envs"


def build_env(
    name: str,
    framework_key: str,
    requirements_path: str | None,
    cfg: Config,
) -> tuple[bool, str]:
    """Create a conda env at /shared/envs/{name} for the given framework.

    Returns (success, env_path). Streams output to console.
    On failure returns (False, "").
    """
    conda_bin = _find_conda(cfg)
    if conda_bin is None:
        err("conda not found.")
        err(f"Expected Miniforge at: {cfg.conda_prefix}")
        info("Install with:")
        info(f"  wget https://github.com/conda-forge/miniforge/releases/latest/download/Miniforge3-Linux-x86_64.sh")
        info(f"  bash Miniforge3-Linux-x86_64.sh -b -p {cfg.conda_prefix}")
        return False, ""

    if framework_key not in FRAMEWORK_PACKAGES:
        err(f"Unknown framework: {framework_key}")
        return False, ""

    # Ensure conda bin is in PATH for this process so pip/python resolve correctly
    conda_bin_dir = str(Path(conda_bin).parent)
    env = {**os.environ, "PATH": f"{conda_bin_dir}:{os.environ.get('PATH', '')}"}

    # Ensure the envs directory exists on the shared filesystem
    envs_root = _envs_root(cfg)
    try:
        envs_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        err(f"Cannot create envs directory {envs_root}: {exc}")
        return False, ""

    env_path = str(envs_root / name)
    pip_path = str(Path(env_path) / "bin" / "pip")

    # Step 1: conda create
    info(f"Creating conda env at {env_path} ...")
    result = subprocess.run(
        [conda_bin, "create", "-p", env_path, "python=3.11", "-y"],
        capture_output=False,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        err("conda create failed.")
        return False, ""

    # Step 2: install framework packages
    packages = FRAMEWORK_PACKAGES[framework_key]
    if packages:
        info(f"Installing {framework_key} packages ...")
        pkg_args = []
        for pkg in packages:
            pkg_args.extend(pkg.split())
        result = subprocess.run(
            [pip_path, "install"] + pkg_args,
            capture_output=False,
            text=True,
        )
        if result.returncode != 0:
            err("pip install failed for framework packages.")
            return False, ""

    # Step 3: install requirements.txt if provided
    if requirements_path:
        info(f"Installing requirements from {requirements_path} ...")
        result = subprocess.run(
            [pip_path, "install", "-r", requirements_path],
            capture_output=False,
            text=True,
        )
        if result.returncode != 0:
            warn("requirements.txt install had errors — env created but may be incomplete.")

    ok(f"Environment {name} ready at {env_path}")
    return True, env_path
