# iitgpu/envbuilder.py
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskID,
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

# Ordered list of candidate conda binary paths searched when conda is not in PATH.
_CONDA_FALLBACK_PATHS = [
    "/shared/miniforge3/bin/conda",
    "/shared/public/miniforge3/bin/conda",
    "/opt/miniforge3/bin/conda",
    str(Path.home() / "miniforge3" / "bin" / "conda"),
    str(Path.home() / "miniconda3" / "bin" / "conda"),
    str(Path.home() / "anaconda3" / "bin" / "conda"),
]

# ── Download stat helpers ──────────────────────────────────────────────────────

# "Downloading torch-2.5.1+cu124-cp311-cp311-linux_x86_64.whl (906.4 MB)"
_DL_HEADER_RE = re.compile(
    r"Downloading\s+(\S+\.(?:whl|tar\.gz|zip))\s+\(([0-9.]+)\s*(B|kB|MB|GB)\)",
    re.IGNORECASE,
)

# "45.3/906.4 MB 47.6 MB/s eta 0:00:18"  — emitted by every pip \r tick
_PROG_RE = re.compile(
    r"(\d+\.?\d*)/(\d+\.?\d*)\s+(B|kB|MB|GB)\s+"
    r"(\d+\.?\d*)\s*(B/s|kB/s|MB/s|GB/s)"
    r"(?:\s+eta\s+(\S+))?",
    re.IGNORECASE,
)

_SIZE_MUL: dict[str, float] = {
    "B": 1.0, "kB": 1_024.0, "MB": 1_048_576.0, "GB": 1_073_741_824.0,
}
_SPEED_MUL: dict[str, float] = {
    "B/s": 1.0, "kB/s": 1_024.0, "MB/s": 1_048_576.0, "GB/s": 1_073_741_824.0,
}


def _to_bytes(val: float, unit: str) -> float:
    return val * _SIZE_MUL.get(unit.upper() if unit.upper() in _SIZE_MUL else unit, 1.0)


def _to_bps(val: float, unit: str) -> float:
    return val * _SPEED_MUL.get(unit, 1.0)


def _fmt_size(b: float) -> str:
    mb = b / 1_048_576
    if mb >= 1_000:
        return f"{mb / 1_024:.2f} GB"
    if mb >= 1:
        return f"{mb:.1f} MB"
    return f"{b / 1_024:.0f} kB"


def _fmt_speed(bps: float) -> str:
    mbps = bps / 1_048_576
    if mbps >= 1_000:
        return f"{mbps / 1_024:.1f} GB/s"
    if mbps >= 1:
        return f"{mbps:.1f} MB/s"
    return f"{bps / 1_024:.0f} kB/s"


def _pkg_display_name(wheel_filename: str) -> str:
    """Return a short, readable package name from a wheel filename."""
    stem = wheel_filename.split("-")[0].replace("_", "-")
    return stem[:42]


# ── conda phase-based progress ─────────────────────────────────────────────────

def _run_with_progress(
    cmd: list[str],
    phases: list[tuple[str, str]],
    label: str,
    env: dict | None = None,
) -> tuple[int, list[str]]:
    """Run *cmd* showing a Rich progress bar driven by conda phase markers."""
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
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            bufsize=1,
        )
        assert proc.stdout is not None

        for raw in proc.stdout:
            line = raw.replace("\r", "").strip()
            if line:
                output_lines.append(line)
            while phase_idx + 1 < n:
                marker, display = phases[phase_idx + 1]
                if marker.lower() in line.lower():
                    prog.advance(task, 1)
                    prog.update(task, description=display)
                    phase_idx += 1
                else:
                    break

        proc.wait()
        remaining = n - (phase_idx + 1)
        if remaining > 0:
            prog.advance(task, remaining)

    return proc.returncode, output_lines


# ── pip per-file download progress ────────────────────────────────────────────

def _run_pip_with_progress(
    cmd: list[str],
    label: str,
    env: dict | None = None,
) -> tuple[int, list[str]]:
    """Run a pip command with per-file download stats: name, size, speed, %.

    pip writes intermediate progress as \\r-delimited segments within each
    \\n-terminated line. Splitting on \\r exposes every tick so the Rich bar
    updates in real time rather than only at 100%.
    """
    output_lines: list[str] = []
    task_map: dict[str, TaskID] = {}   # wheel filename → task id
    current_pkg: str | None = None

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.fields[pkg]:<44}"),
        BarColumn(bar_width=28, complete_style="green", finished_style="bold green"),
        TextColumn("[yellow]{task.fields[sizes]:<24}"),
        TextColumn("[green]{task.fields[speed]:<12}"),
        TextColumn("[dim]{task.fields[eta]}"),
        console=console,
        transient=False,
    ) as prog:
        header_id = prog.add_task(
            label, total=None,
            pkg=f"[bold]{label}[/bold]", sizes="", speed="", eta="",
        )

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        assert proc.stdout is not None

        for raw_line in proc.stdout:
            # Split on \r to get every intermediate pip progress tick,
            # not just the final 100% value that ends the \n-terminated line.
            for seg in raw_line.split("\r"):
                seg = seg.strip()
                if not seg:
                    continue
                output_lines.append(seg)

                # ── New file download starting ─────────────────────────
                m = _DL_HEADER_RE.search(seg)
                if m:
                    filename  = m.group(1)
                    size_val  = float(m.group(2))
                    size_unit = m.group(3)
                    total_b   = _to_bytes(size_val, size_unit)
                    name      = _pkg_display_name(filename)
                    current_pkg = filename

                    tid = prog.add_task(
                        name,
                        total=total_b,
                        pkg=name,
                        sizes=f"0.0 / {_fmt_size(total_b)}",
                        speed="—",
                        eta="...",
                    )
                    task_map[filename] = tid
                    prog.update(header_id, pkg=f"[bold]Downloading  {name}[/bold]")
                    continue

                # ── Live progress tick ─────────────────────────────────
                m = _PROG_RE.search(seg)
                if m and current_pkg and current_pkg in task_map:
                    done_b  = _to_bytes(float(m.group(1)), m.group(3))
                    total_b = _to_bytes(float(m.group(2)), m.group(3))
                    bps     = _to_bps(float(m.group(4)), m.group(5))
                    eta_s   = m.group(6) or ""

                    prog.update(
                        task_map[current_pkg],
                        completed=done_b,
                        total=total_b,
                        sizes=f"{_fmt_size(done_b)} / {_fmt_size(total_b)}",
                        speed=_fmt_speed(bps),
                        eta=f"eta {eta_s}" if eta_s else "[bold green]done[/]",
                    )
                    continue

                # ── Install phase ──────────────────────────────────────
                if "installing collected" in seg.lower():
                    prog.update(header_id, pkg="[bold]Installing packages…[/bold]")

                if "successfully installed" in seg.lower():
                    prog.update(
                        header_id,
                        pkg="[bold green]✔  All packages installed[/bold green]",
                    )

        proc.wait()

    return proc.returncode, output_lines


# ── Error display helper ───────────────────────────────────────────────────────

def _show_error_lines(lines: list[str]) -> None:
    relevant = [l for l in lines if "error" in l.lower() and l.strip()]
    for line in relevant[-10:]:
        console.print(f"  [bold red]{line}[/]")


# ── Conda discovery ────────────────────────────────────────────────────────────

def _find_conda(cfg: Config) -> str | None:
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


# ── Main build function ────────────────────────────────────────────────────────

def build_env(
    name: str,
    framework_key: str,
    requirements_path: str | None,
    cfg: Config,
) -> tuple[bool, str]:
    """Create a conda env at /shared/envs/{name} for the given framework."""
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

    # Route pip cache + temp to /shared (1.7 TB free) so large CUDA wheels
    # (~2-3 GB) don't hit the login VM's per-user home/tmp quota (EDQUOT).
    pip_cache = Path(cfg.nfs_root) / ".pip-cache"
    pip_tmp   = Path(cfg.nfs_root) / ".pip-tmp"
    pip_cache.mkdir(parents=True, exist_ok=True)
    pip_tmp.mkdir(parents=True, exist_ok=True)
    pip_env = {**env, "PIP_CACHE_DIR": str(pip_cache), "TMPDIR": str(pip_tmp)}

    # ── Step 1: conda create ───────────────────────────────────────────────────
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

    # ── Step 2: framework packages via pip ─────────────────────────────────────
    packages = FRAMEWORK_PACKAGES[framework_key]
    if packages:
        info(f"Installing {framework_key} packages ...")
        pkg_args: list[str] = []
        for pkg in packages:
            pkg_args.extend(pkg.split())

        rc, lines = _run_pip_with_progress(
            [pip_path, "install"] + pkg_args,
            f"Installing {framework_key}",
            env=pip_env,
        )
        if rc != 0:
            err("pip install failed for framework packages.")
            _show_error_lines(lines)
            info(f"Index used: {next((a for a in pkg_args if a.startswith('http')), 'default')}")
            info("If the index has no matching wheel, try a different CUDA build or use 'bare' and install manually.")
            return False, ""

    # ── Step 3: requirements.txt ───────────────────────────────────────────────
    if requirements_path:
        info(f"Installing requirements from {requirements_path} ...")
        rc, lines = _run_pip_with_progress(
            [pip_path, "install", "-r", requirements_path],
            "Installing requirements",
            env=pip_env,
        )
        if rc != 0:
            warn("requirements.txt install had errors — env created but may be incomplete.")
            _show_error_lines(lines)

    ok(f"Environment '{name}' ready at {env_path}")
    return True, env_path
