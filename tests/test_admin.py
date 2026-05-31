# tests/test_admin.py
"""Phase 7: admin panel — gating, node control, users, audit."""
from unittest.mock import patch, MagicMock
import json
import pytest
from iitgpu import admin


def _proc(rc=0, out="", err=""):
    m = MagicMock(); m.returncode = rc; m.stdout = out; m.stderr = err
    return m


def test_admin_menu_blocked_for_non_admin(capsys):
    with patch("iitgpu.admin.is_admin", return_value=False):
        admin.admin_menu()   # must return immediately, no prompt
    # no exception = pass; the warn path was taken


def test_drain_node_builds_scontrol_update():
    with patch("subprocess.run", return_value=_proc()) as r:
        ok, msg = admin.drain_node("node1", "maintenance")
    assert ok
    cmd = r.call_args[0][0]
    assert cmd[:3] == ["sudo", "scontrol", "update"]
    assert "nodename=node1" in cmd
    assert "state=drain" in cmd
    assert "reason=maintenance" in cmd


def test_drain_node_requires_reason():
    ok, msg = admin.drain_node("node1", "")
    assert ok is False


def test_resume_node():
    with patch("subprocess.run", return_value=_proc()) as r:
        ok, _ = admin.resume_node("node1")
    assert ok
    assert "state=resume" in r.call_args[0][0]


def test_provision_user_calls_adduser():
    with patch("subprocess.run", return_value=_proc(out="done")) as r:
        ok, _ = admin.provision_user("alice", admin=True)
    cmd = r.call_args[0][0]
    assert cmd[:2] == ["sudo", "iit-gpu-adduser"]
    assert "alice" in cmd and "--admin" in cmd


def test_offboard_user_calls_deluser_with_purge():
    with patch("subprocess.run", return_value=_proc(out="done")) as r:
        ok, _ = admin.offboard_user("bob", purge=True)
    cmd = r.call_args[0][0]
    assert cmd[:2] == ["sudo", "iit-gpu-deluser"]
    assert "--purge-data" in cmd


def test_read_audit_filters(tmp_path, monkeypatch):
    state = tmp_path / "audit.jsonl"
    state.write_text(
        json.dumps({"ts": "2026-05-31T10:00:00", "user": "alice", "action": "job_submit"}) + "\n" +
        json.dumps({"ts": "2026-05-31T10:01:00", "user": "bob", "action": "job_cancel"}) + "\n"
    )
    monkeypatch.setattr(admin, "Path", lambda *a, **k: state if a == ("/var/lib/iit-gpu/audit.jsonl",) else Path(*a))
    # simpler: patch read via the real function pointing at our file
    import iitgpu.admin as A
    with patch.object(A, "read_audit", wraps=A.read_audit):
        pass
    # direct test using the file
    from pathlib import Path as RealPath
    with patch("iitgpu.admin.Path", return_value=state):
        evs = admin.read_audit(action_filter="job_submit")
    assert len(evs) == 1 and evs[0]["user"] == "alice"


def test_list_gpuusers_returns_sorted(monkeypatch):
    fake_grp = MagicMock(gr_mem=["bob", "alice"], gr_gid=1500)
    with patch("grp.getgrnam", return_value=fake_grp), \
         patch("pwd.getpwall", return_value=[]):
        users = admin.list_gpuusers()
    assert users == ["alice", "bob"]
