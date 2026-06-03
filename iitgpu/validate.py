# iitgpu/validate.py
from __future__ import annotations
import os
import re
from pathlib import Path

MAX_GPUS = int(os.environ.get("MAX_GPUS", "8"))
MAX_CPUS = int(os.environ.get("MAX_CPUS", "64"))
MAX_MEM_GB = int(os.environ.get("MAX_MEM_GB", "256"))
MAX_HOURS = int(os.environ.get("MAX_HOURS", "72"))


def _nfs_root() -> str:
    return os.environ.get("NFS_ROOT", "/shared")


def allowed_roots() -> list[str]:
    roots = [str(Path(_nfs_root()).resolve())]
    home = str(Path.home().resolve())
    nfs = roots[0]
    # Only add home if it doesn't subsume the NFS root escape vectors
    if not nfs.startswith(home + os.sep) and nfs != home:
        roots.append(home)
    return roots


def in_jail(path: str) -> bool:
    try:
        real = str(Path(path).resolve())
    except (OSError, ValueError):
        return False
    return any(
        real == root or real.startswith(root + os.sep)
        for root in allowed_roots()
    )


def safe_listdir(path: str) -> list[str]:
    if not in_jail(path):
        return []
    try:
        return os.listdir(path)
    except OSError:
        return []


def clamp_int(value, lo: int, hi: int, default: int) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


_TIME_RE = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)$")


def clean_time_limit(value: str) -> str | None:
    m = _TIME_RE.match(str(value).strip())
    if not m:
        return None
    hours = int(m.group(1))
    mins = m.group(2)
    secs = m.group(3)
    max_h = int(os.environ.get("MAX_HOURS", str(MAX_HOURS)))
    if hours > max_h:
        return f"{max_h:02d}:00:00"
    return f"{hours:02d}:{mins}:{secs}"


_JOB_NAME_RE = re.compile(r"[^A-Za-z0-9._\-]")


def clean_job_name(value: str) -> str:
    return _JOB_NAME_RE.sub("", str(value))[:64]


_MODULE_TOKEN_RE = re.compile(r"[A-Za-z0-9_.+\-/]+")


def clean_modules(value: str) -> list[str]:
    return _MODULE_TOKEN_RE.findall(str(value))[:20]


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def clean_run_command(value: str) -> str:
    return _CONTROL_RE.sub(" ", str(value))[:1000]




# ── Per-user file-access jails (file manager + upload) ───────────────────────

def user_browse_roots(nfs_root: str, username: str) -> list[str]:
    """Directories a regular user may navigate in the file manager.
    Includes their own data dir plus the shared read-only areas."""
    base = str(Path(nfs_root).resolve())
    return [
        str(Path(base) / "users" / username),
        str(Path(base) / "models"),
        str(Path(base) / "envs"),
    ]


def user_upload_root(nfs_root: str, username: str) -> str:
    """The only directory a regular user may write into via upload/file-manager."""
    return str(Path(nfs_root).resolve() / "users" / username)


def in_user_browse_jail(path: str, nfs_root: str, username: str) -> bool:
    """True when path is inside the user's browsable area (own dir + shared models/envs)."""
    try:
        real = str(Path(path).resolve())
    except (OSError, ValueError):
        return False
    return any(
        real == root or real.startswith(root + os.sep)
        for root in user_browse_roots(nfs_root, username)
    )


def in_user_upload_jail(path: str, nfs_root: str, username: str) -> bool:
    """True when path is inside the user's personal upload root (shared/users/<user>)."""
    try:
        real = str(Path(path).resolve())
    except (OSError, ValueError):
        return False
    upload_root = user_upload_root(nfs_root, username)
    return real == upload_root or real.startswith(upload_root + os.sep)
# ── Submit-spec validators (Phase 2) ──────────────────────────────────────────

import re as _re

_ARRAY_RE = _re.compile(r"^\d+(-\d+)?(:\d+)?(,\d+(-\d+)?(:\d+)?)*(%\d+)?$")


def clean_array_spec(value: str) -> str | None:
    """Validate a SLURM --array spec like '0-9', '1-100%4', '1,3,5'.
    Returns the cleaned spec or None if invalid/empty."""
    v = str(value).strip()
    if not v:
        return None
    return v if _ARRAY_RE.match(v) else None


_DEP_RE = _re.compile(
    r"^(after|afterok|afterany|afternotok|aftercorr|singleton)"
    r"(:\d+(_\d+)?)*$"
)


def clean_dependency(value: str) -> str | None:
    """Validate a SLURM --dependency like 'afterok:12345'. Returns None if bad."""
    v = str(value).strip()
    if not v:
        return None
    return v if _DEP_RE.match(v) else None


_SBATCH_RE = re.compile(r'^#SBATCH\s+--?([\w-]+)(?:[=\s](.*))?$')
_PATH_DIRECTIVES = {"output", "error", "chdir", "open-mode"}
_IDENTITY_DIRECTIVES = {"uid", "gid", "get-user-env"}

def validate_sbatch(text: str, username: str, cfg=None) -> list[str]:
    """Parse a sbatch script and return a list of error strings (empty = valid).

    Checks enforced:
    - --output / --error / --chdir paths must resolve inside the NFS jail.
    - --mail-user must be the user's own registered address (if daemon reachable).
    - --uid / --gid / --get-user-env directives are forbidden.
    - Resource directives outside QOS limits produce a friendly pre-flight error.
    """
    if cfg is None:
        from iitgpu.config import load_config
        cfg = load_config()

    errors: list[str] = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("#SBATCH"):
            continue
        m = _SBATCH_RE.match(line)
        if not m:
            continue
        key = m.group(1).lower().rstrip("=")
        val = (m.group(2) or "").strip().strip('"').strip("'")

        # ── Path jail check ────────────────────────────────────────────────
        if key in ("output", "error", "chdir"):
            # Strip SLURM format specifiers (%j, %A, etc.) before resolving
            val_clean = re.sub(r'%[a-zA-Z]', '0', val)
            if val_clean:
                parent = str(Path(val_clean).parent) if key != "chdir" else val_clean
                if not in_jail(parent):
                    errors.append(
                        f"--{key} path is outside the allowed directory: {val!r}"
                    )

        # ── mail-user must be user's own address ───────────────────────────
        elif key == "mail-user":
            if val:
                try:
                    from iitgpu import daemonclient
                    registered = daemonclient.email_for(username)
                    if registered and val.strip().lower() != registered.strip().lower():
                        errors.append(
                            f"--mail-user must be your registered address "
                            f"({registered}); got: {val!r}"
                        )
                except Exception:
                    pass  # daemon unavailable — skip check, SLURM enforces nothing here

        # ── Identity escape directives ─────────────────────────────────────
        elif key in _IDENTITY_DIRECTIVES:
            errors.append(
                f"--{key} is not permitted in submitted scripts"
            )

        # ── Resource pre-flight ────────────────────────────────────────────
        elif key in ("gres", "gpus", "gpus-per-task"):
            try:
                # parse "gpu:N" or plain "N"
                n = int(val.split(":")[-1])
                if n > MAX_GPUS:
                    errors.append(
                        f"--{key} requests {n} GPUs; cluster limit is {MAX_GPUS}"
                    )
            except (ValueError, IndexError):
                pass

        elif key == "cpus-per-task":
            try:
                if int(val) > MAX_CPUS:
                    errors.append(
                        f"--cpus-per-task {val} exceeds cluster limit of {MAX_CPUS}"
                    )
            except ValueError:
                pass

        elif key == "mem":
            try:
                unit = val[-1].upper() if val and val[-1].isalpha() else "M"
                num  = int(val[:-1]) if val and val[-1].isalpha() else int(val)
                mem_gb = num // 1024 if unit == "M" else num if unit == "G" else num * 1024
                if mem_gb > MAX_MEM_GB:
                    errors.append(
                        f"--mem {val} exceeds cluster limit of {MAX_MEM_GB}G"
                    )
            except (ValueError, IndexError):
                pass

    return errors


def validate_against_qos(gpus: int, time_limit: str, max_gpus_per_user: int = 1,
                         max_hours: int | None = None) -> tuple[bool, str]:
    """Reject out-of-policy requests before submission.
    Returns (ok, message). Generic — caller passes the QOS limits."""
    if gpus > max_gpus_per_user:
        return False, (f"Requested {gpus} GPUs but your QOS allows "
                       f"{max_gpus_per_user} per user.")
    if max_hours is not None and time_limit:
        m = _TIME_RE.match(time_limit)
        if m and int(m.group(1)) > max_hours:
            return False, (f"Requested {time_limit} exceeds the QOS wall-time "
                           f"limit of {max_hours}h.")
    return True, "ok"
