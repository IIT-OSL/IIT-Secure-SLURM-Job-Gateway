# tests/test_audit_daemon.py
"""Tests for the audit daemon: SO_PEERCRED identity, users.db CRUD, protocol gating."""
import getpass
import importlib.util
import json
import os
import socket
import sqlite3
import struct
import threading
import time
from pathlib import Path
import pytest


def _load_daemon(tmp_path=None):
    spec = importlib.util.spec_from_file_location(
        "audit_daemon",
        Path(__file__).parent.parent / "deploy" / "audit_daemon.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if tmp_path is not None:
        mod.JSONL_PATH = tmp_path / "audit.jsonl"
    return mod


def _recv_all(sock, n):
    buf = b""
    while len(buf) < n:
        c = sock.recv(n - len(buf))
        if not c:
            return None
        buf += c
    return buf


def _send_req(sock_path: str, verb: str, payload: dict) -> dict:
    req = json.dumps({"verb": verb, "payload": payload}).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(3.0)
        s.connect(sock_path)
        s.sendall(struct.pack(">I", len(req)) + req)
        raw_len = _recv_all(s, 4)
        if not raw_len:
            return {"ok": False, "error": "no response"}
        length = struct.unpack(">I", raw_len)[0]
        data   = _recv_all(s, length)
        return json.loads(data.decode())


@pytest.fixture
def daemon_env(tmp_path, monkeypatch):
    """Live daemon in a thread with temp state dirs; SQLite in check_same_thread=False."""
    ad = _load_daemon(tmp_path)
    # Point JSONL to tmp so daemon threads can write
    ad.JSONL_PATH = tmp_path / "audit.jsonl"
    monkeypatch.setenv("AUDIT_STATE",  str(tmp_path))
    monkeypatch.setenv("AUDIT_SOCKET", str(tmp_path / "test.sock"))
    monkeypatch.setenv("AUDIT_SPOOL",  str(tmp_path / "spool"))

    # check_same_thread=False so the server thread can use connections created here
    audit_conn = sqlite3.connect(str(tmp_path / "audit.db"),
                                 check_same_thread=False)
    ad._init_audit_db(audit_conn)
    users_conn = sqlite3.connect(str(tmp_path / "users.db"),
                                 check_same_thread=False)
    ad._init_users_db(users_conn)

    sock_path = str(tmp_path / "test.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(8)
    server.settimeout(0.2)
    running = [True]

    def _serve():
        while running[0]:
            try:
                conn, _ = server.accept()
                ad._handle_connection(conn, audit_conn, users_conn)
            except socket.timeout:
                continue
            except OSError:
                break
        server.close()

    t = threading.Thread(target=_serve, daemon=True)
    t.start()
    time.sleep(0.05)

    yield ad, audit_conn, users_conn, sock_path

    running[0] = False
    try:
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM).connect(sock_path)
    except OSError:
        pass
    t.join(timeout=2)
    audit_conn.close()
    users_conn.close()


def _dummy_audit_conn(ad, tmp_path=None):
    if tmp_path:
        ad.JSONL_PATH = tmp_path / "audit.jsonl"
    conn = sqlite3.connect(":memory:")
    ad._init_audit_db(conn)
    return conn


# ─── SO_PEERCRED unit tests (no socket needed) ────────────────────────────────

def test_process_event_peer_username_overrides_payload_user(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_audit_db(conn)

    event = {"ts": "2026-01-01T00:00:00+00:00", "user": "forged_user",
             "session": "s", "action": "test_override", "detail": "",
             "job_id": "", "remote": ""}
    ad._process_event(json.dumps(event).encode(), conn, peer_username="real_user")

    row = conn.execute(
        "SELECT user, identity FROM events WHERE action='test_override'"
    ).fetchone()
    assert row[0] == "real_user"
    assert row[1] == "peercred"


def test_process_event_no_peer_marks_spooled(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_audit_db(conn)

    event = {"ts": "2026-01-01T00:00:00+00:00", "user": "alice",
             "session": "s", "action": "spool_action", "detail": "",
             "job_id": "", "remote": ""}
    ad._process_event(json.dumps(event).encode(), conn, peer_username=None)

    row = conn.execute(
        "SELECT user, identity FROM events WHERE action='spool_action'"
    ).fetchone()
    assert row[0] == "alice"    # spooled keeps original user for reference
    assert row[1] == "spooled"


# ─── SO_PEERCRED integration test (real socket, real peer UID) ────────────────

@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_socket_peercred_overrides_forged_user(daemon_env):
    ad, audit_conn, users_conn, sock_path = daemon_env

    resp = _send_req(sock_path, "audit.log", {
        "ts": "2026-01-01T00:00:00+00:00",
        "user":    "forged_user",   # must be overridden by SO_PEERCRED
        "session": "testsession",
        "action":  "peercred_test",
        "detail": "", "job_id": "", "remote": "",
    })
    assert resp.get("ok") is True, resp

    row = audit_conn.execute(
        "SELECT user, identity FROM events WHERE action='peercred_test'"
    ).fetchone()
    real_user = getpass.getuser()
    assert row is not None, "event not stored"
    assert row[0] == real_user, f"Expected {real_user!r}, got {row[0]!r}"
    assert row[1] == "peercred"


# ─── users.db CRUD ────────────────────────────────────────────────────────────

def test_users_db_create_and_get(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_users_db(conn)
    aconn = _dummy_audit_conn(ad, tmp_path)

    ok, data, err = ad._h_users_create(
        {"username": "alice", "email": "alice@example.com", "role": "tool",
         "full_name": "Alice Smith"},
        peer_uid=0, users_conn=conn, audit_conn=aconn,
    )
    assert ok, err
    assert data["username"] == "alice"

    ok2, user, err2 = ad._h_users_get({"username": "alice"}, conn)
    assert ok2, err2
    assert user["email"]  == "alice@example.com"
    assert user["role"]   == "tool"
    assert user["status"] == "active"


def test_users_db_offboard_sets_status_not_delete(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_users_db(conn)
    aconn = _dummy_audit_conn(ad, tmp_path)

    ad._h_users_create(
        {"username": "bob", "email": "bob@x.com", "role": "tool"},
        0, conn, aconn)

    ok, _, err = ad._h_users_offboard({"username": "bob"}, 0, conn, aconn)
    assert ok, err

    # Row still exists, status='offboarded'
    row = conn.execute(
        "SELECT status FROM users WHERE username='bob'"
    ).fetchone()
    assert row is not None, "row was deleted — should be kept"
    assert row[0] == "offboarded"


def test_users_db_offboard_rejects_nonexistent(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_users_db(conn)
    ok, _, err = ad._h_users_offboard({"username": "nobody"}, 0, conn,
                                       _dummy_audit_conn(ad, tmp_path))
    assert not ok


def test_users_db_email_for_self_allowed(tmp_path):
    import pwd
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_users_db(conn)
    aconn = _dummy_audit_conn(ad, tmp_path)

    real_user = getpass.getuser()
    real_uid  = pwd.getpwnam(real_user).pw_uid
    ad._h_users_create(
        {"username": real_user, "email": f"{real_user}@test.com", "role": "tool"},
        0, conn, aconn)

    ok, data, err = ad._h_users_email_for({"username": real_user}, real_uid, conn)
    assert ok, err
    assert data["email"] == f"{real_user}@test.com"


def test_users_db_email_for_other_denied(tmp_path, monkeypatch):
    import pwd
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_users_db(conn)
    aconn = _dummy_audit_conn(ad, tmp_path)

    real_user = getpass.getuser()
    real_uid  = pwd.getpwnam(real_user).pw_uid

    # Force the daemon to see the real user as non-admin
    monkeypatch.setattr(ad, "_uid_is_admin", lambda uid: False)

    ad._h_users_create(
        {"username": "other_person", "email": "other@test.com", "role": "tool"},
        0, conn, aconn)

    ok, _, err = ad._h_users_email_for({"username": "other_person"}, real_uid, conn)
    assert not ok
    assert "permission" in err.lower()


# ─── reconcile ────────────────────────────────────────────────────────────────

def test_reconcile_flags_db_only(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_users_db(conn)
    aconn = _dummy_audit_conn(ad, tmp_path)

    ad._h_users_create(
        {"username": "ghost_user_99999",
         "email": "ghost@test.com", "role": "tool"},
        0, conn, aconn)

    _, data, _ = ad._h_users_reconcile(conn)
    assert "ghost_user_99999" in data["db_only"]


# ─── admin gating (socket level) ──────────────────────────────────────────────

@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_non_admin_cannot_create_user(daemon_env, monkeypatch):
    ad, audit_conn, users_conn, sock_path = daemon_env
    monkeypatch.setenv("ADMIN_GROUP", "__nonexistent_group_xyz__")

    resp = _send_req(sock_path, "users.create",
                     {"username": "testuser", "email": "t@t.com", "role": "tool"})
    assert resp.get("ok") is False
    assert "permission" in resp.get("error", "").lower()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_non_admin_cannot_query_audit(daemon_env, monkeypatch):
    ad, audit_conn, users_conn, sock_path = daemon_env
    monkeypatch.setenv("ADMIN_GROUP", "__nonexistent_group_xyz__")

    resp = _send_req(sock_path, "audit.query", {})
    assert resp.get("ok") is False
    assert "permission" in resp.get("error", "").lower()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"),
                    reason="Unix sockets not available")
def test_non_admin_cannot_read_joblog(daemon_env, monkeypatch):
    ad, audit_conn, users_conn, sock_path = daemon_env
    monkeypatch.setenv("ADMIN_GROUP", "__nonexistent_group_xyz__")

    resp = _send_req(sock_path, "joblog.read",
                     {"user": "alice", "filename": "slurm-1.out"})
    assert resp.get("ok") is False
    assert "permission" in resp.get("error", "").lower()


# ─── audit_query filters ──────────────────────────────────────────────────────

def test_audit_query_filters_by_user(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_audit_db(conn)
    ts = "2026-06-01T00:00:00+00:00"
    for user, action in [("alice", "job_submit"), ("bob", "job_cancel")]:
        conn.execute(
            "INSERT INTO events (ts,user,session,action,detail,job_id,remote) "
            "VALUES (?,?,?,?,?,?,?)", (ts, user, "s", action, "", "", ""))
    conn.commit()

    ok, data, _ = ad._h_audit_query({"user": "alice"}, peer_uid=0, audit_conn=conn)
    events = data["events"]
    assert all(e["user"] == "alice" for e in events)
    assert len(events) == 1


def test_audit_query_filters_by_action(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_audit_db(conn)
    ts = "2026-06-01T00:00:00+00:00"
    for user, action in [("alice", "job_submit"), ("bob", "job_cancel")]:
        conn.execute(
            "INSERT INTO events (ts,user,session,action,detail,job_id,remote) "
            "VALUES (?,?,?,?,?,?,?)", (ts, user, "s", action, "", "", ""))
    conn.commit()

    ok, data, _ = ad._h_audit_query({"action": "job_cancel"}, peer_uid=0,
                                     audit_conn=conn)
    events = data["events"]
    assert len(events) == 1
    assert events[0]["action"] == "job_cancel"


def test_audit_query_filters_by_date_range(tmp_path):
    ad   = _load_daemon(tmp_path)
    conn = sqlite3.connect(":memory:")
    ad._init_audit_db(conn)
    for ts, user in [
        ("2026-01-01T00:00:00+00:00", "alice"),
        ("2026-06-01T00:00:00+00:00", "bob"),
    ]:
        conn.execute(
            "INSERT INTO events (ts,user,session,action,detail,job_id,remote) "
            "VALUES (?,?,?,?,?,?,?)", (ts, user, "s", "act", "", "", ""))
    conn.commit()

    ok, data, _ = ad._h_audit_query(
        {"date_from": "2026-03-01T00:00:00+00:00"}, peer_uid=0, audit_conn=conn)
    assert len(data["events"]) == 1
    assert data["events"][0]["user"] == "bob"


# ─── admin read self-audit ────────────────────────────────────────────────────

def test_admin_read_user_log_emits_audit_event(tmp_path, monkeypatch):
    ad    = _load_daemon(tmp_path)
    aconn = _dummy_audit_conn(ad, tmp_path)

    jobs_dir = tmp_path / "jobs" / "testuser"
    jobs_dir.mkdir(parents=True)
    (jobs_dir / "slurm-1.out").write_text("output here")

    import getpass, pwd
    real_user = getpass.getuser()
    real_uid  = pwd.getpwnam(real_user).pw_uid

    monkeypatch.setenv("NFS_ROOT",    str(tmp_path))
    monkeypatch.setenv("JOBS_SUBDIR", "jobs")

    ok, data, err = ad._h_joblog_read(
        {"user": "testuser", "filename": "slurm-1.out"},
        real_uid, aconn)
    assert ok, err
    assert "output here" in data["content"]

    row = aconn.execute(
        "SELECT action, detail FROM events WHERE action='admin_read_user_log'"
    ).fetchone()
    assert row is not None, "admin_read_user_log event not recorded"
    assert row[1] == "testuser"
