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
