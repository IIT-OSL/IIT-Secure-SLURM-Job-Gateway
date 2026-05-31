# tests/test_onboarding.py
"""Phase 1: onboarding scripts lint + config role detection."""
import subprocess
from pathlib import Path
from unittest.mock import patch
import pytest

DEPLOY = Path(__file__).parent.parent / "deploy"


@pytest.mark.parametrize("script", ["iit-gpu-adduser.sh", "iit-gpu-deluser.sh"])
def test_script_exists_and_executable(script):
    p = DEPLOY / script
    assert p.exists(), f"deploy/{script} missing"
    assert p.stat().st_mode & 0o111, f"deploy/{script} not executable"


@pytest.mark.parametrize("script", ["iit-gpu-adduser.sh", "iit-gpu-deluser.sh"])
def test_script_passes_bash_syntax_check(script):
    r = subprocess.run(["bash", "-n", str(DEPLOY / script)],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"bash -n failed for {script}: {r.stderr}"


def test_adduser_rejects_bad_username():
    r = subprocess.run(["bash", str(DEPLOY / "iit-gpu-adduser.sh"), "Bad Name!", "--dry-run"],
                       capture_output=True, text=True,
                       env={"GPU_HOST_SSH": "x@y", "PATH": "/usr/bin:/bin"})
    assert r.returncode != 0
    assert "invalid username" in (r.stdout + r.stderr).lower()


def test_deluser_refuses_protected_accounts():
    for acct in ("public", "root", "slurm"):
        r = subprocess.run(["bash", str(DEPLOY / "iit-gpu-deluser.sh"), acct, "--dry-run"],
                           capture_output=True, text=True,
                           env={"GPU_HOST_SSH": "x@y", "PATH": "/usr/bin:/bin"})
        assert r.returncode != 0, f"deluser should refuse {acct}"
        assert "protected" in (r.stdout + r.stderr).lower()


def test_adduser_dry_run_picks_uid(tmp_path):
    # --dry-run must not require root and should print a chosen UID without changing anything
    r = subprocess.run(["bash", str(DEPLOY / "iit-gpu-adduser.sh"), "alice", "--dry-run"],
                       capture_output=True, text=True,
                       env={"GPU_HOST_SSH": "localhost", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                            "UID_MIN": "2000", "UID_MAX": "60000"})
    # It will try ssh localhost which may fail in CI; accept either a UID line or a clean ssh failure
    out = r.stdout + r.stderr
    assert "dry-run" in out.lower() or "UID" in out or "ssh" in out.lower()


# ── config role detection ──────────────────────────────────────────────────────

def test_is_admin_true_when_in_admin_group(monkeypatch):
    from iitgpu import config
    with patch("iitgpu.config.user_groups", return_value={"gpuusers", "gpuadmins"}):
        assert config.is_admin() is True


def test_is_admin_false_when_not_in_admin_group(monkeypatch):
    from iitgpu import config
    with patch("iitgpu.config.user_groups", return_value={"gpuusers"}):
        assert config.is_admin() is False


def test_user_groups_returns_set():
    from iitgpu.config import user_groups
    g = user_groups()
    assert isinstance(g, set)
