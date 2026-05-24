# tests/test_auditclient.py
import json
import os
import socket
import threading
import time
from pathlib import Path
import pytest


def _start_stub_daemon(sock_path: str, received: list) -> threading.Thread:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(sock_path)
    sock.settimeout(2.0)

    def _run():
        try:
            while True:
                try:
                    data, _ = sock.recvfrom(65535)
                    received.append(json.loads(data.decode()))
                except socket.timeout:
                    break
        finally:
            sock.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
def test_log_sends_three_events(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "audit.sock")
    monkeypatch.setenv("AUDIT_SOCKET", sock_path)
    monkeypatch.setenv("AUDIT_SPOOL", str(tmp_path / "spool"))

    received: list = []
    t = _start_stub_daemon(sock_path, received)
    time.sleep(0.05)

    import importlib
    import iitgpu.auditclient as ac
    importlib.reload(ac)

    ac.log("job_submit", detail="test1", job_id="J001")
    ac.log("job_cancel", detail="test2", job_id="J002")
    ac.log("session_end", detail="test3", job_id="")

    time.sleep(0.1)
    t.join(timeout=3)

    assert len(received) == 3
    actions = [e["action"] for e in received]
    assert "job_submit" in actions
    assert "job_cancel" in actions
    assert "session_end" in actions

    submit_event = next(e for e in received if e["action"] == "job_submit")
    assert submit_event["job_id"] == "J001"
    assert "user" in submit_event
    assert "ts" in submit_event
    assert "session" in submit_event


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
def test_log_spools_when_socket_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "nonexistent.sock"))
    spool_dir = tmp_path / "spool"
    monkeypatch.setenv("AUDIT_SPOOL", str(spool_dir))

    import importlib
    import iitgpu.auditclient as ac
    importlib.reload(ac)

    result = ac.log("test_action", detail="spooled", job_id="J999")
    assert result is True
    spool_files = list(spool_dir.iterdir())
    assert len(spool_files) == 1
    event = json.loads(spool_files[0].read_text())
    assert event["action"] == "test_action"
    assert event["job_id"] == "J999"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets not available")
def test_log_or_block_returns_false_when_both_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "bad.sock"))
    # Use an existing file path as the spool dir — mkdir will fail
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("AUDIT_SPOOL", str(blocker))

    import importlib
    import iitgpu.auditclient as ac
    importlib.reload(ac)

    result = ac.log_or_block("job_submit", detail="blocked")
    assert result is False
