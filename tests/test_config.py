import pytest
from iitgpu.config import Config, load_config, jobs_dir


def test_defaults(monkeypatch):
    for k in ("NFS_ROOT", "JOBS_SUBDIR", "DEMO_MODE"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.nfs_root == "/shared"
    assert cfg.jobs_subdir == "jobs"
    assert cfg.demo_mode is False


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("NFS_ROOT", "/data/nfs")
    monkeypatch.setenv("JOBS_SUBDIR", "myjobs")
    monkeypatch.setenv("DEMO_MODE", "1")
    cfg = load_config()
    assert cfg.nfs_root == "/data/nfs"
    assert cfg.jobs_subdir == "myjobs"
    assert cfg.demo_mode is True


def test_demo_mode_only_on_exact_1(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "true")
    cfg = load_config()
    assert cfg.demo_mode is False


def test_jobs_dir_joins_root_and_subdir():
    cfg = Config(nfs_root="/shared", jobs_subdir="jobs", demo_mode=False, conda_prefix="/shared/miniforge3", sacct_enabled=False)
    assert jobs_dir(cfg) == "/shared/jobs"


def test_jobs_dir_custom():
    cfg = Config(nfs_root="/data", jobs_subdir="slurm_jobs", demo_mode=False, conda_prefix="/shared/miniforge3", sacct_enabled=False)
    assert jobs_dir(cfg) == "/data/slurm_jobs"
