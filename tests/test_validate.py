# tests/test_validate.py
import os
from pathlib import Path
import pytest


def _set_nfs(monkeypatch, path: str) -> None:
    monkeypatch.setenv("NFS_ROOT", path)


# ── in_jail ──────────────────────────────────────────────────────────────────

def test_in_jail_accepts_file_under_root(tmp_path, monkeypatch):
    _set_nfs(monkeypatch, str(tmp_path))
    target = tmp_path / "data" / "file.txt"
    target.parent.mkdir(parents=True)
    target.write_text("hi")
    from iitgpu.validate import in_jail
    assert in_jail(str(target)) is True


def test_in_jail_rejects_escape_dotdot(tmp_path, monkeypatch):
    _set_nfs(monkeypatch, str(tmp_path))
    escape = str(tmp_path) + "/../other"
    from iitgpu.validate import in_jail
    assert in_jail(escape) is False


def test_in_jail_rejects_etc_shadow(tmp_path, monkeypatch):
    _set_nfs(monkeypatch, str(tmp_path))
    from iitgpu.validate import in_jail
    assert in_jail("/etc/shadow") is False


def test_in_jail_rejects_symlink_escape(tmp_path, monkeypatch):
    _set_nfs(monkeypatch, str(tmp_path))
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available on this platform")
    from iitgpu.validate import in_jail
    assert in_jail(str(link)) is False


# ── safe_listdir ─────────────────────────────────────────────────────────────

def test_safe_listdir_inside_jail(tmp_path, monkeypatch):
    _set_nfs(monkeypatch, str(tmp_path))
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "b.txt").write_text("b")
    from iitgpu.validate import safe_listdir
    result = safe_listdir(str(tmp_path))
    assert set(result) == {"a.txt", "b.txt"}


def test_safe_listdir_outside_jail_returns_empty(tmp_path, monkeypatch):
    _set_nfs(monkeypatch, str(tmp_path))
    from iitgpu.validate import safe_listdir
    assert safe_listdir("/etc") == []


# ── clamp_int ────────────────────────────────────────────────────────────────

def test_clamp_int_within_range():
    from iitgpu.validate import clamp_int
    assert clamp_int(4, 1, 8, 1) == 4


def test_clamp_int_caps_high():
    from iitgpu.validate import clamp_int
    assert clamp_int(9999, 1, 8, 1) == 8


def test_clamp_int_floors_low():
    from iitgpu.validate import clamp_int
    assert clamp_int(0, 1, 8, 1) == 1


def test_clamp_int_uses_default_on_bad_type():
    from iitgpu.validate import clamp_int
    assert clamp_int("bad", 1, 8, 3) == 3  # type: ignore


# ── clean_time_limit ─────────────────────────────────────────────────────────

def test_clean_time_limit_valid(monkeypatch):
    monkeypatch.setenv("MAX_HOURS", "72")
    from iitgpu.validate import clean_time_limit
    assert clean_time_limit("12:30:00") == "12:30:00"


def test_clean_time_limit_clamps_over_max(monkeypatch):
    monkeypatch.setenv("MAX_HOURS", "72")
    from iitgpu.validate import clean_time_limit
    result = clean_time_limit("999:00:00")
    assert result == "72:00:00"


def test_clean_time_limit_rejects_garbage():
    from iitgpu.validate import clean_time_limit
    assert clean_time_limit("not-a-time") is None


def test_clean_time_limit_rejects_bad_minutes():
    from iitgpu.validate import clean_time_limit
    assert clean_time_limit("01:99:00") is None


# ── clean_job_name ────────────────────────────────────────────────────────────

def test_clean_job_name_strips_bad_chars():
    from iitgpu.validate import clean_job_name
    assert clean_job_name("my job!@#") == "myjob"


def test_clean_job_name_allows_safe_chars():
    from iitgpu.validate import clean_job_name
    assert clean_job_name("train_v1.2-run") == "train_v1.2-run"


def test_clean_job_name_truncates_at_64():
    from iitgpu.validate import clean_job_name
    assert len(clean_job_name("a" * 100)) == 64


# ── clean_run_command ─────────────────────────────────────────────────────────

def test_clean_run_command_removes_newlines():
    from iitgpu.validate import clean_run_command
    result = clean_run_command("python train.py\nrm -rf /")
    assert "\n" not in result


def test_clean_run_command_removes_control_chars():
    from iitgpu.validate import clean_run_command
    result = clean_run_command("python\x00train.py\x1b[31m")
    assert "\x00" not in result
    assert "\x1b" not in result


def test_clean_run_command_truncates_at_1000():
    from iitgpu.validate import clean_run_command
    assert len(clean_run_command("x" * 2000)) == 1000


# ── validate_sbatch ───────────────────────────────────────────────────────────

def _user_dir(tmp_path, user="alice"):
    """Create and return the user's own workspace under tmp_path NFS root."""
    d = tmp_path / "users" / user
    d.mkdir(parents=True, exist_ok=True)
    return d


def _v(text, tmp_path, username="alice", email="alice@iit.lk"):
    """Run validate_sbatch with NFS_ROOT set and email_for mocked."""
    from unittest.mock import patch
    import importlib, iitgpu.validate as v
    os.environ["NFS_ROOT"] = str(tmp_path)
    importlib.reload(v)
    with patch("iitgpu.daemonclient.email_for", return_value=email):
        return v.validate_sbatch(text, username)


def test_validate_sbatch_clean_script_passes(tmp_path):
    ud = _user_dir(tmp_path)
    script = f"""#!/bin/bash
#SBATCH --job-name=test
#SBATCH --output={ud}/slurm-%j.out
#SBATCH --error={ud}/slurm-%j.err
#SBATCH --chdir={ud}
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
python train.py
"""
    assert _v(script, tmp_path) == []


def test_validate_sbatch_rejects_output_outside_jail(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --output=/etc/malicious.out\n", tmp_path)
    assert any("--output" in e for e in errors)


def test_validate_sbatch_rejects_chdir_outside_jail(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --chdir=/etc\n", tmp_path)
    assert any("--chdir" in e for e in errors)


def test_validate_sbatch_rejects_cross_user_output(tmp_path):
    """M1: alice must not be able to target bob's workspace (per-user jail)."""
    _user_dir(tmp_path, "alice")
    bob = _user_dir(tmp_path, "bob")
    errors = _v(f"#SBATCH --output={bob}/stolen.out\n", tmp_path, username="alice")
    assert any("--output" in e for e in errors), \
        "cross-user output path must be rejected by per-user jail"


def test_validate_sbatch_multiflag_bypass_blocked(tmp_path):
    """H1: a dangerous flag hidden after a benign one on the same line must be caught."""
    _user_dir(tmp_path)
    errors = _v("#SBATCH --nice=100 --output=/etc/cron.d/evil\n", tmp_path)
    assert any("--output" in e for e in errors), \
        "multi-directive line must not bypass the path check"


def test_validate_sbatch_multiflag_uid_blocked(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --job-name=ok --uid=0\n", tmp_path)
    assert any("--uid" in e for e in errors)


def test_validate_sbatch_rejects_uid_directive(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --uid=0\n", tmp_path)
    assert any("--uid" in e for e in errors)


def test_validate_sbatch_rejects_gid_directive(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --gid=0\n", tmp_path)
    assert any("--gid" in e for e in errors)


def test_validate_sbatch_mail_user_fail_closed_when_daemon_down(tmp_path):
    """L1: if the daemon can't confirm the address, a non-empty --mail-user is rejected."""
    from unittest.mock import patch
    import importlib, iitgpu.validate as v
    os.environ["NFS_ROOT"] = str(tmp_path)
    _user_dir(tmp_path)
    importlib.reload(v)
    with patch("iitgpu.daemonclient.email_for", side_effect=Exception("daemon down")):
        errors = v.validate_sbatch("#SBATCH --mail-user=attacker@evil.com\n", "alice")
    assert any("mail-user" in e for e in errors)


def test_validate_sbatch_mail_user_own_address_ok(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --mail-user=alice@iit.lk\n", tmp_path,
                username="alice", email="alice@iit.lk")
    assert errors == []


def test_validate_sbatch_mail_user_foreign_rejected(tmp_path):
    _user_dir(tmp_path)
    errors = _v("#SBATCH --mail-user=someone@else.com\n", tmp_path,
                username="alice", email="alice@iit.lk")
    assert any("mail-user" in e for e in errors)


def test_validate_sbatch_rejects_excess_gpus(tmp_path):
    os.environ.update({"NFS_ROOT": str(tmp_path), "MAX_GPUS": "2"})
    _user_dir(tmp_path)
    import importlib, iitgpu.validate as v; importlib.reload(v)
    from unittest.mock import patch
    with patch("iitgpu.daemonclient.email_for", return_value="alice@iit.lk"):
        errors = v.validate_sbatch("#SBATCH --gres=gpu:8\n", "alice")
    assert any("GPU" in e for e in errors)
    os.environ.pop("MAX_GPUS", None)


def test_validate_sbatch_rejects_excess_cpus(tmp_path):
    os.environ.update({"NFS_ROOT": str(tmp_path), "MAX_CPUS": "4"})
    _user_dir(tmp_path)
    import importlib, iitgpu.validate as v; importlib.reload(v)
    from unittest.mock import patch
    with patch("iitgpu.daemonclient.email_for", return_value="alice@iit.lk"):
        errors = v.validate_sbatch("#SBATCH --cpus-per-task=32\n", "alice")
    assert any("cpus" in e.lower() for e in errors)
    os.environ.pop("MAX_CPUS", None)


def test_validate_sbatch_no_false_positives_on_comments(tmp_path):
    """Lines that are plain comments (not #SBATCH) must not trigger errors."""
    _user_dir(tmp_path)
    script = "# This is a comment mentioning --uid and --gid\npython x.py\n"
    assert _v(script, tmp_path) == []


def test_validate_sbatch_ignores_indented_sbatch(tmp_path):
    """Indented #SBATCH is ignored by SLURM and by us (no false positive)."""
    _user_dir(tmp_path)
    assert _v("   #SBATCH --output=/etc/x\n", tmp_path) == []
