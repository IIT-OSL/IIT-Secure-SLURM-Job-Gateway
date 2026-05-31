# tests/test_monitor_completeness.py
"""Phase 3: hold/release/requeue, job detail+seff, history filters."""
from unittest.mock import MagicMock, patch
import pytest
from iitgpu import slurm


def _run_ok(stdout="", rc=0):
    m = MagicMock(); m.returncode = rc; m.stdout = stdout; m.stderr = ""
    return m


def test_hold_calls_scontrol_hold(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch("subprocess.run", return_value=_run_ok()) as r:
        ok, msg = slurm.hold("123")
    assert ok and "hold" in msg.lower()
    assert "hold" in r.call_args[0][0]
    assert "123" in r.call_args[0][0]


def test_release_calls_scontrol_release(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch("subprocess.run", return_value=_run_ok()) as r:
        ok, _ = slurm.release("123")
    assert ok and "release" in r.call_args[0][0]


def test_requeue_calls_scontrol_requeue(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch("subprocess.run", return_value=_run_ok()) as r:
        ok, _ = slurm.requeue("123")
    assert ok
    assert "requeue" in r.call_args[0][0]


def test_hold_reports_failure(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    m = MagicMock(); m.returncode = 1; m.stdout = ""; m.stderr = "Invalid job id"
    with patch("subprocess.run", return_value=m):
        ok, msg = slurm.hold("999")
    assert ok is False
    assert "Invalid job id" in msg


def test_job_detail_returns_scontrol_output(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch("subprocess.run", return_value=_run_ok("JobId=123 JobState=RUNNING")):
        out = slurm.job_detail("123")
    assert "JobId=123" in out


def test_job_efficiency_returns_seff(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch("subprocess.run", return_value=_run_ok("CPU Efficiency: 90.0%")):
        out = slurm.job_efficiency("123")
    assert "Efficiency" in out


def test_job_efficiency_handles_missing_seff(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with patch("subprocess.run", side_effect=FileNotFoundError):
        out = slurm.job_efficiency("123")
    assert "not installed" in out.lower()


def test_filtered_history_state_filter(monkeypatch, tmp_path):
    monkeypatch.setenv("SACCT_ENABLED", "1")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    out = (
        "1|a|u|COMPLETED|00:05:00|s|e|\n"
        "2|b|u|FAILED|00:01:00|s|e|\n"
    )
    with patch("subprocess.run", return_value=_run_ok(out)):
        rows = slurm.filtered_history(str(tmp_path), state="FAILED")
    assert all(r.state == "FAILED" for r in rows)
    assert len(rows) == 1


def test_filtered_history_all_users_adds_a_flag(monkeypatch, tmp_path):
    monkeypatch.setenv("SACCT_ENABLED", "1")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    with patch("subprocess.run", return_value=_run_ok("")) as r:
        slurm.filtered_history(str(tmp_path), all_users=True)
    assert "-a" in r.call_args[0][0]


def test_demo_mode_actions_are_safe(monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "1")
    assert slurm.hold("1")[0] is True
    assert slurm.requeue("1")[0] is True
    assert "JobId" in slurm.job_detail("1")
