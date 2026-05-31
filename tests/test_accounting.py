# tests/test_accounting.py
"""Phase 4: usage/accounting reports."""
from unittest.mock import patch, MagicMock
import pytest
from iitgpu import accounting
from iitgpu.accounting import _elapsed_to_hours, usage_by_user, fairshare


def test_elapsed_to_hours_basic():
    assert _elapsed_to_hours("01:00:00") == 1.0
    assert _elapsed_to_hours("00:30:00") == 0.5
    assert _elapsed_to_hours("02:15:00") == pytest.approx(2.25)


def test_elapsed_to_hours_with_days():
    assert _elapsed_to_hours("1-00:00:00") == 24.0
    assert _elapsed_to_hours("2-12:00:00") == 60.0


def test_elapsed_to_hours_garbage_is_zero():
    assert _elapsed_to_hours("n/a") == 0.0
    assert _elapsed_to_hours("") == 0.0


def _mock(stdout):
    m = MagicMock(); m.returncode = 0; m.stdout = stdout; m.stderr = ""
    return m


def test_usage_by_user_aggregates_gpu_and_cpu_hours():
    out = (
        "alice|01:00:00|cpu=4,gres/gpu=1,mem=8G|COMPLETED\n"
        "alice|02:00:00|cpu=8,gres/gpu=1,mem=16G|COMPLETED\n"
        "bob|00:30:00|cpu=2,mem=4G|COMPLETED\n"
    )
    with patch("subprocess.run", return_value=_mock(out)):
        rows = usage_by_user(days=30)
    by = {r.user: r for r in rows}
    assert by["alice"].gpu_hours == pytest.approx(3.0)      # 1h*1 + 2h*1
    assert by["alice"].cpu_hours == pytest.approx(1*4 + 2*8)
    assert by["alice"].job_count == 2
    assert by["bob"].gpu_hours == 0.0
    assert by["bob"].cpu_hours == pytest.approx(0.5*2)


def test_usage_by_user_sorted_by_gpu_hours_desc():
    out = (
        "low|01:00:00|cpu=1,gres/gpu=1|COMPLETED\n"
        "high|05:00:00|cpu=1,gres/gpu=1|COMPLETED\n"
    )
    with patch("subprocess.run", return_value=_mock(out)):
        rows = usage_by_user()
    assert rows[0].user == "high"


def test_usage_by_user_empty():
    with patch("subprocess.run", return_value=_mock("")):
        assert usage_by_user() == []


def test_fairshare_parses_rows():
    out = "alice|100|0.523\nbob|100|0.211\n"
    with patch("subprocess.run", return_value=_mock(out)):
        rows = fairshare()
    assert ("alice", "100", "0.523") in rows
    assert len(rows) == 2


def test_sreport_unavailable_message():
    m = MagicMock(); m.returncode = 1; m.stdout = ""; m.stderr = ""
    with patch("subprocess.run", return_value=m):
        out = accounting.sreport_cluster_usage()
    assert "unavailable" in out.lower()
