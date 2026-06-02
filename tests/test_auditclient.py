# tests/test_auditclient.py
"""Tests for auditclient — stream socket, spool, and daemon_request."""
import getpass
import importlib
import json
import socket
import struct
import threading
import time
from pathlib import Path

import pytest


# ─── stream-socket stub helpers ───────────────────────────────────────────────

def _recv_all(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def _start_stub_server(sock_path: str, received: list,
                       stop: threading.Event) -> threading.Thread:
    """STREAM server: reads one request per connection, appends payload to received."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)
    server.settimeout(0.2)

    def _run():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            try:
                conn.settimeout(2.0)
                raw_len = _recv_all(conn, 4)
                if not raw_len:
                    continue
                length = struct.unpack(">I", raw_len)[0]
                data   = _recv_all(conn, length)
                if data:
                    req = json.loads(data.decode())
                    received.append(req.get("payload", req))
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        server.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


# ─── basic send / spool ───────────────────────────────────────────────────────

@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_log_sends_three_events(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "audit.sock")
    monkeypatch.setenv("AUDIT_SOCKET", sock_path)
    monkeypatch.setenv("AUDIT_SPOOL",  str(tmp_path / "spool"))

    received: list = []
    stop = threading.Event()
    t = _start_stub_server(sock_path, received, stop)
    time.sleep(0.05)

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    ac.log("job_submit",  detail="test1", job_id="J001")
    ac.log("job_cancel",  detail="test2", job_id="J002")
    ac.log("session_end", detail="test3")

    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)

    assert len(received) == 3
    actions = [e["action"] for e in received]
    assert "job_submit"  in actions
    assert "job_cancel"  in actions
    assert "session_end" in actions

    submit = next(e for e in received if e["action"] == "job_submit")
    assert submit["job_id"] == "J001"
    assert "user"    in submit
    assert "ts"      in submit
    assert "session" in submit


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_log_includes_meta(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "audit.sock")
    monkeypatch.setenv("AUDIT_SOCKET", sock_path)
    monkeypatch.setenv("AUDIT_SPOOL",  str(tmp_path / "spool"))

    received: list = []
    stop = threading.Event()
    t = _start_stub_server(sock_path, received, stop)
    time.sleep(0.05)

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    ac.log("job_submit", meta={"run_command": "python train.py",
                               "model_path": "/shared/models/llm"})
    time.sleep(0.2)
    stop.set()
    t.join(timeout=2)

    assert len(received) == 1
    assert received[0].get("meta", {}).get("run_command") == "python train.py"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_log_spools_when_socket_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "nonexistent.sock"))
    spool_dir = tmp_path / "spool"
    monkeypatch.setenv("AUDIT_SPOOL", str(spool_dir))

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    result = ac.log("test_action", detail="spooled", job_id="J999")
    assert result is True
    spool_files = list(spool_dir.iterdir())
    assert len(spool_files) == 1
    event = json.loads(spool_files[0].read_text())
    assert event["action"]   == "test_action"
    assert event["job_id"]   == "J999"
    assert event["identity"] == "spooled"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_log_or_block_spools_when_socket_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "bad.sock"))
    spool_dir = tmp_path / "spool"
    monkeypatch.setenv("AUDIT_SPOOL", str(spool_dir))

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    result = ac.log_or_block("job_submit", detail="no_daemon")
    assert result is True
    assert len(list(spool_dir.iterdir())) == 1


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_log_or_block_returns_false_when_both_fail(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "bad.sock"))
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    monkeypatch.setenv("AUDIT_SPOOL", str(blocker))

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    result = ac.log_or_block("job_submit", detail="blocked")
    assert result is False


# ─── daemon_request protocol ──────────────────────────────────────────────────

def _start_echo_server(sock_path: str,
                       stop: threading.Event) -> threading.Thread:
    """STREAM server that sends back {"ok": true, "data": {"echo": verb}}."""
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(4)
    server.settimeout(0.2)

    def _run():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except socket.timeout:
                continue
            try:
                conn.settimeout(2.0)
                raw_len = _recv_all(conn, 4)
                if not raw_len:
                    continue
                length = struct.unpack(">I", raw_len)[0]
                data   = _recv_all(conn, length)
                req    = json.loads(data.decode())
                resp   = json.dumps({"ok": True,
                                     "data": {"echo": req.get("verb")}}).encode()
                conn.sendall(struct.pack(">I", len(resp)) + resp)
            except Exception:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
        server.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_daemon_request_returns_response(tmp_path, monkeypatch):
    sock_path = str(tmp_path / "echo.sock")
    monkeypatch.setenv("AUDIT_SOCKET", sock_path)

    stop = threading.Event()
    t = _start_echo_server(sock_path, stop)
    time.sleep(0.05)

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    resp = ac.daemon_request("users.list", {})
    stop.set()
    t.join(timeout=2)

    assert resp["ok"] is True
    assert resp["data"]["echo"] == "users.list"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_daemon_request_returns_error_on_no_daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "no.sock"))

    import iitgpu.auditclient as ac
    importlib.reload(ac)

    resp = ac.daemon_request("users.list", {})
    assert resp["ok"] is False
    assert "error" in resp
