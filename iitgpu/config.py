import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    nfs_root: str
    jobs_subdir: str
    demo_mode: bool


def load_config() -> Config:
    return Config(
        nfs_root=os.environ.get("NFS_ROOT", "/shared"),
        jobs_subdir=os.environ.get("JOBS_SUBDIR", "jobs"),
        demo_mode=os.environ.get("DEMO_MODE", "0") == "1",
    )


def jobs_dir(cfg: Config) -> str:
    return str(Path(cfg.nfs_root) / cfg.jobs_subdir).replace("\\", "/")


def models_dir(cfg: Config) -> str:
    return str(Path(cfg.nfs_root) / "models").replace("\\", "/")


def templates_dir(cfg: Config) -> str:
    return str(Path(cfg.nfs_root) / "templates").replace("\\", "/")
