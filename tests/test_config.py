import pytest
from iitgpu.config import Config, load_config, jobs_dir


def _cfg(**over):
    base = dict(
        nfs_root="/shared", jobs_subdir="jobs", conda_prefix="/shared/miniforge3",
        demo_mode=False, sacct_enabled=False,
        gpuusers_group="gpuusers", admin_group="gpuadmins",
        default_account="default", default_qos="normal", partition="gpu",
        shared_user="daham", gateway_shared_user=False,
        gateway_host="localhost", gateway_port="22",
    )
    base.update(over)
    return Config(**base)


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
    cfg = _cfg(nfs_root="/shared", jobs_subdir="jobs")
    assert jobs_dir(cfg) == "/shared/jobs"


def test_jobs_dir_custom():
    cfg = _cfg(nfs_root="/data", jobs_subdir="slurm_jobs")
    assert jobs_dir(cfg) == "/data/slurm_jobs"


# ── Phase 0: site.env loading + new knobs ──────────────────────────────────────

def test_site_env_provides_defaults(tmp_path, monkeypatch):
    site = tmp_path / "site.env"
    site.write_text("GATEWAY_HOST=gw.example.edu\nGATEWAY_PORT=2222\nGPUUSERS_GROUP=labusers\n")
    monkeypatch.setenv("IIT_SITE_ENV", str(site))
    for k in ("GATEWAY_HOST", "GATEWAY_PORT", "GPUUSERS_GROUP"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.gateway_host == "gw.example.edu"
    assert cfg.gateway_port == "2222"
    assert cfg.gpuusers_group == "labusers"


def test_real_env_overrides_site_env(tmp_path, monkeypatch):
    site = tmp_path / "site.env"
    site.write_text("GATEWAY_HOST=from-file\n")
    monkeypatch.setenv("IIT_SITE_ENV", str(site))
    monkeypatch.setenv("GATEWAY_HOST", "from-env")
    assert load_config().gateway_host == "from-env"


def test_missing_site_env_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("IIT_SITE_ENV", str(tmp_path / "does-not-exist.env"))
    for k in ("GATEWAY_HOST", "GPUUSERS_GROUP"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.gpuusers_group == "gpuusers"     # built-in default
    assert cfg.gateway_host == "localhost"


def test_gateway_shared_user_flag_parsing(tmp_path, monkeypatch):
    monkeypatch.setenv("IIT_SITE_ENV", str(tmp_path / "none.env"))
    monkeypatch.setenv("GATEWAY_SHARED_USER", "1")
    assert load_config().gateway_shared_user is True
    monkeypatch.setenv("GATEWAY_SHARED_USER", "0")
    assert load_config().gateway_shared_user is False


def test_new_config_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("IIT_SITE_ENV", str(tmp_path / "none.env"))
    for k in ("GPUUSERS_GROUP","ADMIN_GROUP","SLURM_ACCOUNT","SLURM_QOS",
              "SLURM_PARTITION","GATEWAY_SHARED_USER","GATEWAY_HOST","GATEWAY_PORT"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config()
    assert cfg.admin_group == "gpuadmins"
    assert cfg.default_account == "default"
    assert cfg.default_qos == "normal"
    assert cfg.partition == "gpu"
    assert cfg.gateway_shared_user is False     # per-user identity is the repo default
