# tests/test_shell.py
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def test_allowed_commands_list_contains_expected():
    from iitgpu.shell import ALLOWED_COMMANDS
    assert "sbatch" in ALLOWED_COMMANDS
    assert "squeue" in ALLOWED_COMMANDS
    assert "scancel" in ALLOWED_COMMANDS
    assert "sinfo" in ALLOWED_COMMANDS
    assert "tail" in ALLOWED_COMMANDS


def test_blocked_command_is_not_in_allowed():
    from iitgpu.shell import ALLOWED_COMMANDS
    assert "bash" not in ALLOWED_COMMANDS
    assert "rm" not in ALLOWED_COMMANDS
    assert "python" not in ALLOWED_COMMANDS


def test_parse_command_splits_correctly():
    from iitgpu.shell import _parse_command
    cmd, args = _parse_command("sbatch /shared/daham/job.sh")
    assert cmd == "sbatch"
    assert args == ["/shared/daham/job.sh"]


def test_parse_command_handles_empty_string():
    from iitgpu.shell import _parse_command
    cmd, args = _parse_command("   ")
    assert cmd == ""
    assert args == []


def test_dispatch_blocks_disallowed_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.shell import _dispatch
    _dispatch("bash", ["-c", "rm -rf /"])
    captured = capsys.readouterr()
    assert "not allowed" in captured.out.lower() or "not allowed" in captured.err.lower()


def test_dispatch_sbatch_rejects_path_outside_jail(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.shell import _dispatch
    _dispatch("sbatch", ["/etc/passwd"])
    captured = capsys.readouterr()
    assert "denied" in captured.out.lower() or "denied" in captured.err.lower()


def test_dispatch_tail_rejects_path_outside_jail(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.shell import _dispatch
    _dispatch("tail", ["-f", "/etc/shadow"])
    captured = capsys.readouterr()
    assert "denied" in captured.out.lower() or "denied" in captured.err.lower()


def test_dispatch_sbatch_accepts_path_inside_jail(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    script = tmp_path / "job.sh"
    script.write_text("#!/bin/bash\necho hi")

    run_calls = []
    def fake_run(cmd, **kwargs):
        run_calls.append(cmd)
        m = MagicMock()
        m.returncode = 0
        return m

    with patch("subprocess.run", side_effect=fake_run), \
         patch("iitgpu.auditclient.log", return_value=True):
        from iitgpu.shell import _dispatch
        import importlib, iitgpu.shell as sh
        importlib.reload(sh)
        sh._dispatch("sbatch", [str(script)])

    assert any("sbatch" in str(c) for c in run_calls)
