# iitgpu/envbuilder.py
from __future__ import annotations

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
    if shutil.which("conda") is None:
        err("conda not found in PATH.")
        info("Install Miniforge: https://github.com/conda-forge/miniforge/releases")
        return False, ""

    if framework_key not in FRAMEWORK_PACKAGES:
        err(f"Unknown framework: {framework_key}")
        return False, ""

    env_path = str(_envs_root(cfg) / name)
    pip_path = str(Path(env_path) / "bin" / "pip")

    # Step 1: conda create
    info(f"Creating conda env at {env_path} ...")
    result = subprocess.run(
        ["conda", "create", "-p", env_path, "python=3.11", "-y"],
        capture_output=False,
        text=True,
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

    ok(f"Environment '{name}' ready at {env_path}")
    return True, env_path
