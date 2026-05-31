import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    nfs_root: str
    jobs_subdir: str
    demo_mode: bool
    conda_prefix: str
    sacct_enabled: bool  # True → use sacct for job history; False → file-scan fallback


def _probe_sacct() -> bool:
    """Return True if sacct is on PATH (indicates slurmdbd is available)."""
    return shutil.which("sacct") is not None


def load_config() -> Config:
    raw = os.environ.get("SACCT_ENABLED", "auto").strip().lower()
    if raw == "auto":
        sacct = _probe_sacct()
    else:
        sacct = raw in ("1", "true", "yes")

    return Config(
        nfs_root=os.environ.get("NFS_ROOT", "/shared"),
        jobs_subdir=os.environ.get("JOBS_SUBDIR", "jobs"),
        demo_mode=os.environ.get("DEMO_MODE", "0") == "1",
        conda_prefix=os.environ.get("CONDA_PREFIX_SHARED", "/shared/miniforge3"),
        sacct_enabled=sacct,
    )


def jobs_dir(cfg: Config) -> str:
    return str(Path(cfg.nfs_root) / cfg.jobs_subdir).replace("\\", "/")


def models_dir(cfg: Config) -> str:
    return str(Path(cfg.nfs_root) / "models").replace("\\", "/")


def templates_dir(cfg: Config) -> str:
    return str(Path(cfg.nfs_root) / "templates").replace("\\", "/")


def conda_sh(cfg: Config) -> str:
    """Absolute path to conda.sh for sourcing in sbatch scripts and subprocesses."""
    return str(Path(cfg.conda_prefix) / "etc" / "profile.d" / "conda.sh")
