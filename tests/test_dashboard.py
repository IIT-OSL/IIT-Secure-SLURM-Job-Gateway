# tests/test_dashboard.py
from pathlib import Path
import pytest


def test_get_log_tail_returns_empty_when_file_missing():
    from iitgpu.dashboard import _get_log_tail
    result = _get_log_tail("/nonexistent/path/slurm-999.out", lines=20)
    assert result == []


def test_get_log_tail_returns_last_n_lines(tmp_path):
    log = tmp_path / "slurm-1.out"
    log.write_text("\n".join(f"line {i}" for i in range(50)))
    from iitgpu.dashboard import _get_log_tail
    result = _get_log_tail(str(log), lines=20)
    assert len(result) == 20
    assert result[-1] == "line 49"
    assert result[0] == "line 30"


def test_get_log_tail_returns_all_lines_when_file_is_short(tmp_path):
    log = tmp_path / "slurm-2.out"
    log.write_text("line 1\nline 2\nline 3")
    from iitgpu.dashboard import _get_log_tail
    result = _get_log_tail(str(log), lines=20)
    assert result == ["line 1", "line 2", "line 3"]


def test_find_job_log_returns_none_when_no_file(tmp_path):
    from iitgpu.dashboard import _find_job_log
    result = _find_job_log("99999", str(tmp_path))
    assert result is None


def test_find_job_log_finds_matching_file(tmp_path):
    log = tmp_path / "slurm-42.out"
    log.write_text("output")
    from iitgpu.dashboard import _find_job_log
    result = _find_job_log("42", str(tmp_path))
    assert result == str(log)
