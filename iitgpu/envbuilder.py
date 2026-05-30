# iitgpu/envbuilder.py
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from rich.console import Group
from rich.live import Live
from rich.markup import escape as _markup_escape
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.text import Text

from iitgpu.config import Config
from iitgpu.ui import console, err, info, ok, warn

# pip install args per framework key.
#
# RTX 5090 (sm_120 / Blackwell) requires PyTorch 2.6+.
# PyTorch 2.5 and earlier have no compiled kernels for sm_120 — CUDA will
# appear available but crash on the first kernel call.  Driver backward
# compatibility only covers CUDA runtime ABI, not GPU architecture support.
FRAMEWORK_PACKAGES: dict[str, list[str]] = {
    "pytorch-2.6": [
        "torch==2.6.* torchvision torchaudio "
        "--index-url https://download.pytorch.org/whl/cu126"
    ],
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
    "pytorch-2.6":     "PyTorch 2.6  (CUDA 12.6 — RTX 5090 / sm_120 native) [recommended]",
    "pytorch-2.5":     "PyTorch 2.5  (CUDA 12.4 — no RTX 5090 sm_120 support)",
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

# Lowercase keys so lookups are case-insensitive (pip may write kB or KB)
_SIZE_MUL: dict[str, float] = {
    "b": 1.0, "kb": 1_024.0, "mb": 1_048_576.0, "gb": 1_073_741_824.0,
}
_SPEED_MUL: dict[str, float] = {
    "b/s": 1.0, "kb/s": 1_024.0, "mb/s": 1_048_576.0, "gb/s": 1_073_741_824.0,
}


def _to_bytes(val: float, unit: str) -> float:
    return val * _SIZE_MUL.get(unit.lower(), 1.0)


def _to_bps(val: float, unit: str) -> float:
    return val * _SPEED_MUL.get(unit.lower(), 1.0)


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

def _pip_log_text(log_lines: list[str]) -> Text:
    """Render the last 3 pip log lines as dim text below the progress bar."""
    shown = log_lines[-3:] if log_lines else []
    # Pad to 3 lines so the display height stays constant and doesn't jump
    while len(shown) < 3:
        shown.insert(0, "")
    lines = [f"  {_markup_escape(l):<100}" for l in shown]
    return Text("\n".join(lines), style="dim", no_wrap=True)


def _run_pip_with_progress(
    cmd: list[str],
    label: str,
    env: dict | None = None,
) -> tuple[int, list[str]]:
    """Run a pip install with a live progress bar + rolling 3-line log window.

    Layout (updates in place, no scrolling)::

        ⠋  torch           ━━━━━━━━━━━━╸  452.1 / 906.4 MB  47.6 MB/s  eta 9s
          Collecting nvidia-nccl-cu12==2.21.5 (from torch==2.5.*)
          Downloading nvidia_nccl_cu12-2.21.5-py3-none-manylinux2014_x86_64.whl
          Progress 14680064 of 188743680

    The three log lines replace themselves each tick — no accumulation.
    During the "Linking" phase (no measurable progress) the log shows each
    package name being linked so it's clear work is happening.
    """
    import time as _time

    # Insert --progress-bar raw right after "install"
    raw_cmd: list[str] = []
    inserted = False
    for arg in cmd:
        raw_cmd.append(arg)
        if arg == "install" and not inserted:
            # --progress-bar raw: machine-readable Progress X of Y\n lines
            # -v: verbose output so the log window shows activity during linking
            # --no-compile: skip .pyc generation at install time — Python
            #   compiles lazily on first import. Saves thousands of NFS writes
            #   (one per .py file) and cuts NFS linking time by ~40-60%.
            raw_cmd += ["--progress-bar", "raw", "-v", "--no-compile"]
            inserted = True

    pip_env = {**(env or {}), "PYTHONUNBUFFERED": "1"}

    output_lines:    list[str] = []
    log_lines:       list[str] = []   # rolling display buffer (last 3 shown)
    current_pkg:     str | None = None
    current_total_b: float = 0.0

    # Speed tracking — exponential moving average
    _t_last: float = 0.0
    _b_last: float = 0.0
    _speed:  float = 0.0

    def _reset_speed() -> None:
        nonlocal _t_last, _b_last, _speed
        _t_last = _time.monotonic()
        _b_last = 0.0
        _speed  = 0.0

    def _tick_speed(done_b: float) -> float:
        nonlocal _t_last, _b_last, _speed
        now = _time.monotonic()
        dt  = now - _t_last
        if dt >= 0.2:
            inst   = (done_b - _b_last) / dt
            _speed = 0.25 * inst + 0.75 * _speed if _speed > 0 else inst
            _t_last = now
            _b_last = done_b
        return _speed

    # Build Progress separately so we can embed it inside Live + Group
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[bold cyan]{task.fields[pkg]:<38}"),
        BarColumn(bar_width=26, complete_style="green", finished_style="bold green"),
        TextColumn("[yellow]{task.fields[sizes]:<22}"),
        TextColumn("[green]{task.fields[speed]:<11}"),
        TextColumn("[dim]{task.fields[eta]:<12}"),
        TimeElapsedColumn(),
        console=console,
    )
    file_task = progress.add_task(
        label, total=100,
        pkg=f"[bold]{label}[/bold]", sizes="", speed="", eta="",
    )

    def _render() -> Group:
        return Group(progress, _pip_log_text(log_lines))

    with Live(_render(), console=console, refresh_per_second=12,
              transient=False, vertical_overflow="crop") as live:

        proc = subprocess.Popen(
            raw_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=pip_env,
        )
        assert proc.stdout is not None

        while True:
            raw = proc.stdout.readline()
            if not raw:
                break
            seg = raw.decode("utf-8", errors="replace").strip()
            if not seg:
                continue
            output_lines.append(seg)

            # ── Progress X of Y ────────────────────────────────────────────
            if seg.startswith("Progress "):
                parts = seg.split()
                if len(parts) == 4:
                    try:
                        done_b  = float(parts[1])
                        total_b = float(parts[3])
                        spd     = _tick_speed(done_b)
                        remaining = total_b - done_b
                        eta_s = (
                            f"{int(remaining / spd)}s"
                            if spd > 1 and remaining > 0 else ""
                        )
                        current_total_b = total_b
                        progress.update(
                            file_task,
                            completed=done_b,
                            total=total_b,
                            sizes=f"{_fmt_size(done_b)} / {_fmt_size(total_b)}",
                            speed=_fmt_speed(spd) if spd > 0 else "—",
                            eta=f"eta {eta_s}" if eta_s else "",
                        )
                    except (ValueError, ZeroDivisionError):
                        pass
                # Don't add raw "Progress X of Y" to the visible log
                live.update(_render())
                continue

            # ── Everything else goes into the rolling log window ────────────
            log_lines.append(seg)

            # ── New file download starting ──────────────────────────────────
            m = _DL_HEADER_RE.search(seg)
            if m:
                if current_pkg:
                    console.print(
                        f"  [bold green]✔[/]  {current_pkg:<42}"
                        f"  [dim]{_fmt_size(current_total_b)}[/]"
                    )
                filename        = m.group(1)
                current_total_b = _to_bytes(float(m.group(2)), m.group(3))
                current_pkg     = _pkg_display_name(filename)
                _reset_speed()
                progress.update(
                    file_task,
                    completed=0,
                    total=max(current_total_b, 1),
                    pkg=current_pkg,
                    sizes=f"0 / {_fmt_size(current_total_b)}",
                    speed="—",
                    eta="...",
                )
                live.update(_render())
                continue

            # ── Linking phase ───────────────────────────────────────────────
            if "installing collected" in seg.lower():
                if current_pkg:
                    console.print(
                        f"  [bold green]✔[/]  {current_pkg:<42}"
                        f"  [dim]{_fmt_size(current_total_b)}[/]"
                    )
                    current_pkg = None
                console.print(
                    "  [bold yellow]⚠[/]  Linking packages to NFS — "
                    "this can take [bold]15–30 min[/bold] for large CUDA installs. "
                    "Do not interrupt."
                )
                progress.update(
                    file_task,
                    completed=0, total=None,
                    pkg="[bold yellow]Linking to NFS…[/bold yellow]",
                    sizes="", speed="", eta="",
                )

            # Count files being linked so the display shows active progress
            if "changing mode of" in seg or "copying" in seg.lower():
                _link_count = getattr(_run_pip_with_progress, "_link_count", 0) + 1
                _run_pip_with_progress._link_count = _link_count  # type: ignore[attr-defined]
                progress.update(
                    file_task,
                    pkg=f"[bold yellow]Linking to NFS…  ({_link_count} files)[/bold yellow]",
                )

            if "successfully installed" in seg.lower():
                _run_pip_with_progress._link_count = 0  # type: ignore[attr-defined]
                progress.update(
                    file_task,
                    completed=1, total=1,
                    pkg="[bold green]✔  All packages installed[/bold green]",
                    sizes="", speed="", eta="",
                )

            live.update(_render())

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
