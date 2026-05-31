# tests/test_slurm.py
"""Tests for slurm.py — sacct_history, job_history, recent_jobs fallback."""
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


# ── sacct_history ─────────────────────────────────────────────────────────────

def _make_sacct_output(*rows):
    """Build parsable2 sacct output. Each row: (jobid, name, user, state, elapsed)."""
    lines = []
    for r in rows:
        jid, name, user, state, elapsed = r
        lines.append(f"{jid}|{name}|{user}|{state}|{elapsed}|2026-05-30T10:00:00|2026-05-30T11:00:00|gres/gpu=1")
    return "\n".join(lines)


def test_sacct_history_parses_completed_jobs():
    from iitgpu.slurm import sacct_history, QueueEntry

    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = _make_sacct_output(
        ("100", "train", "daham", "COMPLETED", "01:00:00"),
        ("101", "infer", "daham", "FAILED", "00:10:00"),
    )
    with patch("subprocess.run", return_value=mock):
        rows = sacct_history(limit=10)
    assert len(rows) == 2
    assert rows[0].job_id == "101"   # newest-first (reversed)
    assert rows[1].job_id == "100"
    assert rows[0].state == "FAILED"
    assert rows[1].state == "COMPLETED"


def test_sacct_history_skips_step_lines():
    from iitgpu.slurm import sacct_history

    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = (
        "200|train|daham|COMPLETED|02:00:00|2026-05-30T10:00:00|2026-05-30T12:00:00|gres/gpu=1\n"
        "200.batch|batch|daham|COMPLETED|02:00:00|2026-05-30T10:00:00|2026-05-30T12:00:00|\n"
    )
    with patch("subprocess.run", return_value=mock):
        rows = sacct_history()
    assert len(rows) == 1
    assert rows[0].job_id == "200"


def test_sacct_history_returns_empty_on_failure():
    from iitgpu.slurm import sacct_history

    mock = MagicMock()
    mock.returncode = 1
    mock.stdout = ""
    with patch("subprocess.run", return_value=mock):
        assert sacct_history() == []


def test_sacct_history_returns_empty_on_oserror():
    from iitgpu.slurm import sacct_history

    with patch("subprocess.run", side_effect=OSError("no sacct")):
        assert sacct_history() == []


def test_sacct_history_respects_limit():
    from iitgpu.slurm import sacct_history

    rows_data = [
        (str(i), f"job{i}", "daham", "COMPLETED", "00:01:00")
        for i in range(50)
    ]
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = _make_sacct_output(*rows_data)
    with patch("subprocess.run", return_value=mock):
        result = sacct_history(limit=5)
    assert len(result) == 5


def test_sacct_history_strips_state_suffix():
    """State 'CANCELLED by 1234' should be stripped to 'CANCELLED'."""
    from iitgpu.slurm import sacct_history

    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = "300|job|daham|CANCELLED by 1234|00:05:00|2026-05-30T10:00:00|2026-05-30T10:05:00|\n"
    with patch("subprocess.run", return_value=mock):
        rows = sacct_history()
    assert rows[0].state == "CANCELLED"


# ── job_history ───────────────────────────────────────────────────────────────

def test_job_history_uses_sacct_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "1")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = _make_sacct_output(("400", "sacct_job", "daham", "COMPLETED", "00:30:00"))

    with patch("subprocess.run", return_value=mock):
        from iitgpu.slurm import job_history
        rows = job_history(str(tmp_path))
    assert any(r.job_id == "400" for r in rows)


def test_job_history_falls_back_to_file_scan_when_sacct_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "0")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    # Create a fake job output file
    job_dir = tmp_path / "jobs" / "daham" / "train_20260530_120000"
    job_dir.mkdir(parents=True)
    (job_dir / "slurm-500.out").write_text("output\n")

    from iitgpu.slurm import job_history
    rows = job_history(str(tmp_path))
    assert any(r.job_id == "500" for r in rows)


def test_job_history_falls_back_when_sacct_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "1")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    # sacct returns nothing
    mock = MagicMock()
    mock.returncode = 0
    mock.stdout = ""

    job_dir = tmp_path / "jobs" / "daham" / "train_20260530_130000"
    job_dir.mkdir(parents=True)
    (job_dir / "slurm-600.out").write_text("output\n")

    with patch("subprocess.run", return_value=mock):
        from iitgpu.slurm import job_history
        rows = job_history(str(tmp_path))
    assert any(r.job_id == "600" for r in rows)


# ── config.SACCT_ENABLED ──────────────────────────────────────────────────────

def test_config_sacct_enabled_explicit_true(monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "1")
    import importlib
    import iitgpu.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg = cfg_mod.load_config()
    assert cfg.sacct_enabled is True


def test_config_sacct_enabled_explicit_false(monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "0")
    import importlib
    import iitgpu.config as cfg_mod
    importlib.reload(cfg_mod)
    cfg = cfg_mod.load_config()
    assert cfg.sacct_enabled is False


def test_config_sacct_auto_detects_via_which(monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "auto")
    with patch("shutil.which", return_value="/usr/bin/sacct"):
        import importlib
        import iitgpu.config as cfg_mod
        importlib.reload(cfg_mod)
        cfg = cfg_mod.load_config()
    assert cfg.sacct_enabled is True


def test_config_sacct_auto_returns_false_when_sacct_missing(monkeypatch):
    monkeypatch.setenv("SACCT_ENABLED", "auto")
    with patch("shutil.which", return_value=None):
        import importlib
        import iitgpu.config as cfg_mod
        importlib.reload(cfg_mod)
        cfg = cfg_mod.load_config()
    assert cfg.sacct_enabled is False
