"""Runtime configuration.

All site-specific values (paths, hostnames, ports, group/account names) live in
environment variables so the repo stays generic and open-source friendly.

Layering (lowest priority first):
  1. built-in defaults below
  2. deploy/site.env  (KEY=VALUE lines; git-ignored; copy from site.env.example)
  3. real environment variables (highest priority)

The repo runs on a different cluster by editing only deploy/site.env.
"""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

# Location of the optional site.env file. Override with IIT_SITE_ENV.
_DEFAULT_SITE_ENV = "/opt/iit-gpu/deploy/site.env"


def _load_site_env() -> dict[str, str]:
    """Parse deploy/site.env into a dict. Real env vars still take precedence.

    Format: plain KEY=VALUE lines; blank lines and # comments ignored.
    Never raises — a missing or malformed file just yields {}.
    """
    path = os.environ.get("IIT_SITE_ENV", _DEFAULT_SITE_ENV)
    data: dict[str, str] = {}
    try:
        for raw in Path(path).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            # strip optional surrounding quotes
            val = val.strip().strip('"').strip("'")
            if key:
                data[key] = val
    except (OSError, ValueError):
        pass
    return data


# Loaded once at import; refreshed by load_config() so tests that set env win.
_SITE = _load_site_env()


def _get(key: str, default: str) -> str:
    """Resolve a config key: real env var > site.env > built-in default."""
    if key in os.environ:
        return os.environ[key]
    if key in _SITE:
        return _SITE[key]
    return default


@dataclass(frozen=True)
class Config:
    # Storage
    nfs_root: str
    jobs_subdir: str
    conda_prefix: str
    # Behaviour
    demo_mode: bool
    sacct_enabled: bool
    # Identity / SLURM
    gpuusers_group: str       # POSIX group that gates the gateway + owns job dirs
    admin_group: str          # POSIX group whose members see the admin panel
    default_account: str      # SLURM account for normal users
    default_qos: str          # SLURM QOS for normal users
    partition: str            # default SLURM partition
    shared_user: str          # legacy shared SLURM user (e.g. "daham")
    gateway_shared_user: bool # True → run SLURM as shared_user via sudo (legacy)
    # Gateway / tunnels (for notebook & service SSH hints)
    gateway_host: str         # public-facing SSH host users tunnel to
    gateway_port: str         # public-facing SSH port


def _truthy(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes", "on")


def _probe_sacct() -> bool:
    return shutil.which("sacct") is not None


def load_config() -> Config:
    # Refresh site.env each call so tests that point IIT_SITE_ENV elsewhere work.
    global _SITE
    _SITE = _load_site_env()

    raw = _get("SACCT_ENABLED", "auto").strip().lower()
    sacct = _probe_sacct() if raw == "auto" else raw in ("1", "true", "yes")

    return Config(
        nfs_root=_get("NFS_ROOT", "/shared"),
        jobs_subdir=_get("JOBS_SUBDIR", "jobs"),
        conda_prefix=_get("CONDA_PREFIX_SHARED", "/shared/miniforge3"),
        demo_mode=_get("DEMO_MODE", "0") == "1",
        sacct_enabled=sacct,
        gpuusers_group=_get("GPUUSERS_GROUP", "gpuusers"),
        admin_group=_get("ADMIN_GROUP", "gpuadmins"),
        default_account=_get("SLURM_ACCOUNT", "default"),
        default_qos=_get("SLURM_QOS", "normal"),
        partition=_get("SLURM_PARTITION", "gpu"),
        shared_user=_get("GATEWAY_SHARED_USER_NAME", "daham"),
        gateway_shared_user=_truthy(_get("GATEWAY_SHARED_USER", "0")),
        gateway_host=_get("GATEWAY_HOST", "localhost"),
        gateway_port=_get("GATEWAY_PORT", "22"),
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
