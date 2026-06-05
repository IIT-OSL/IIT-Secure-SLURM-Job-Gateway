# tests/test_daemon_verbs.py
"""Phase 4 — test coverage for daemon verb handlers added in M05.

Tests exercise the handler functions directly with in-memory SQLite DBs,
so they run without a live daemon socket.
"""
import importlib.util
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def _isolate_mail_flag(tmp_path, monkeypatch):
    """Point the daemon mail kill-switch at a clean per-test dir so the real
    /shared/.mail-disabled (live operational state) can never affect these tests."""
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))


def _load_daemon():
    spec = importlib.util.spec_from_file_location(
        "audit_daemon",
        Path(__file__).parent.parent / "deploy" / "audit_daemon.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _users_db():
    d = _load_daemon()
    conn = sqlite3.connect(":memory:")
    d._init_users_db(conn)
    return d, conn


def _audit_db():
    d = _load_daemon()
    conn = sqlite3.connect(":memory:")
    d._init_audit_db(conn)
    return conn


def _insert_user(conn, username, role="tool", status="active",
                 email="u@iit.lk", must_change_pw=0):
    conn.execute(
        "INSERT INTO users (username,uid,full_name,email,role,status,"
        "created_at,created_by,notes,must_change_pw) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (username, 9000, "", email, role, status,
         datetime.now(timezone.utc).isoformat(), "test", "", must_change_pw),
    )
    conn.commit()


# ── users.create upsert ───────────────────────────────────────────────────────

def test_create_inserts_new_user():
    d, conn = _users_db()
    aconn = _audit_db()
    ok, data, err = d._h_users_create(
        {"username": "alice", "email": "alice@iit.lk", "role": "tool"},
        9000, conn, aconn,
    )
    assert ok, err
    row = conn.execute("SELECT status FROM users WHERE username='alice'").fetchone()
    assert row and row[0] == "active"


def test_create_reactivates_offboarded_user():
    d, conn = _users_db()
    aconn = _audit_db()
    _insert_user(conn, "alice", status="offboarded")
    ok, data, err = d._h_users_create(
        {"username": "alice", "email": "new@iit.lk", "role": "admin"},
        9000, conn, aconn,
    )
    assert ok, err
    row = conn.execute("SELECT status, email, role FROM users WHERE username='alice'").fetchone()
    assert row[0] == "active"
    assert row[1] == "new@iit.lk"
    assert row[2] == "admin"


def test_create_rejects_active_duplicate():
    d, conn = _users_db()
    aconn = _audit_db()
    _insert_user(conn, "alice", status="active")
    ok, _, err = d._h_users_create(
        {"username": "alice", "email": "x@iit.lk", "role": "tool"},
        9000, conn, aconn,
    )
    assert not ok
    assert "already exists and is active" in err


def test_create_stores_must_change_pw_flag():
    d, conn = _users_db()
    aconn = _audit_db()
    ok, _, err = d._h_users_create(
        {"username": "bob", "email": "b@iit.lk", "role": "tool", "must_change_pw": True},
        9000, conn, aconn,
    )
    assert ok, err
    row = conn.execute("SELECT must_change_pw FROM users WHERE username='bob'").fetchone()
    assert row[0] == 1


def test_create_must_change_pw_defaults_to_zero():
    d, conn = _users_db()
    aconn = _audit_db()
    d._h_users_create(
        {"username": "carol", "email": "c@iit.lk", "role": "tool"},
        9000, conn, aconn,
    )
    row = conn.execute("SELECT must_change_pw FROM users WHERE username='carol'").fetchone()
    assert row[0] == 0


# ── users.admin_emails ────────────────────────────────────────────────────────

def test_admin_emails_returns_only_active_admins():
    d, conn = _users_db()
    _insert_user(conn, "admin1", role="admin", status="active",  email="a1@iit.lk")
    _insert_user(conn, "admin2", role="admin", status="offboarded", email="a2@iit.lk")
    _insert_user(conn, "user1",  role="tool",  status="active",  email="u1@iit.lk")
    ok, data, _ = d._h_users_admin_emails(conn)
    assert ok
    emails = data["emails"]
    assert "a1@iit.lk" in emails
    assert "a2@iit.lk" not in emails   # offboarded
    assert "u1@iit.lk" not in emails   # not admin


def test_admin_emails_excludes_blank_emails():
    d, conn = _users_db()
    _insert_user(conn, "adminX", role="admin", status="active", email="")
    ok, data, _ = d._h_users_admin_emails(conn)
    assert "" not in data["emails"]


# ── users.check_must_change_pw / users.clear_must_change_pw ──────────────────

def test_check_must_change_pw_true_when_set():
    d, conn = _users_db()
    _insert_user(conn, "eve", must_change_pw=1)
    d._uid_to_username = lambda uid: "eve"
    ok, data, _ = d._h_users_check_must_change_pw(
        {"username": "eve"}, peer_uid=9000, users_conn=conn)
    assert ok
    assert data["must_change_pw"] is True


def test_check_must_change_pw_false_when_not_set():
    d, conn = _users_db()
    _insert_user(conn, "frank", must_change_pw=0)
    d._uid_to_username = lambda uid: "frank"
    ok, data, _ = d._h_users_check_must_change_pw(
        {"username": "frank"}, peer_uid=9000, users_conn=conn)
    assert ok
    assert data["must_change_pw"] is False


def test_clear_must_change_pw_resets_flag():
    d, conn = _users_db()
    _insert_user(conn, "grace", must_change_pw=1)
    d._uid_to_username = lambda uid: "grace"
    ok, _, _ = d._h_users_clear_must_change_pw(
        {"username": "grace"}, peer_uid=9000, users_conn=conn)
    assert ok
    row = conn.execute("SELECT must_change_pw FROM users WHERE username='grace'").fetchone()
    assert row[0] == 0


def test_check_must_change_pw_permission_denied_for_other_user():
    """A non-admin peer can only check their own flag."""
    d, conn = _users_db()
    _insert_user(conn, "hank", must_change_pw=0)
    # peer_uid resolves to "other" — not matching "hank" and not admin
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(d, "_uid_to_username", lambda uid: "other")
        mp.setattr(d, "_uid_is_admin", lambda uid: False)
        ok, _, err = d._h_users_check_must_change_pw(
            {"username": "hank"}, peer_uid=5000, users_conn=conn)
    assert not ok
    assert "permission denied" in err.lower()


# ── schema migration ──────────────────────────────────────────────────────────

def test_schema_migration_adds_must_change_pw_column():
    """_init_users_db must add must_change_pw even to old DBs that lack it."""
    d = _load_daemon()
    conn = sqlite3.connect(":memory:")
    # Create table without must_change_pw (simulates old schema)
    conn.execute("""
        CREATE TABLE users (
            username TEXT PRIMARY KEY, uid INTEGER, full_name TEXT,
            email TEXT NOT NULL, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL, created_by TEXT NOT NULL, notes TEXT
        )
    """)
    conn.commit()
    # Running init should add the column without error
    d._init_users_db(conn)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    assert "must_change_pw" in cols


# ── timezone conversion ───────────────────────────────────────────────────────

def test_admin_fmt_ts_utc_to_lk():
    from iitgpu.admin import _fmt_ts
    result = _fmt_ts("2026-06-03T00:00:00+00:00")
    assert result == "2026-06-03 05:30:00"


def test_admin_fmt_ts_handles_z_suffix():
    from iitgpu.admin import _fmt_ts
    assert _fmt_ts("2026-06-03T00:00:00Z") == "2026-06-03 05:30:00"


def test_job_folder_uses_lk_time(tmp_path, monkeypatch):
    """make_job_folder timestamp must reflect LK time, not UTC."""
    from datetime import timezone, timedelta
    lk = timezone(timedelta(hours=5, minutes=30))
    fixed_lk = datetime(2026, 6, 3, 12, 0, 0, tzinfo=lk)

    import iitgpu.jobs as jobs_mod
    monkeypatch.setattr(
        jobs_mod, "_LK", lk,
        raising=False,
    )

    from unittest.mock import patch
    from iitgpu.jobs import JobSpec, make_job_folder
    spec = JobSpec(job_name="test", partition="gpu", gpus=1, cpus=4,
                   mem_gb=8, time_limit="01:00:00", run_command="python x.py")
    with patch("iitgpu.jobs.datetime") as mock_dt:
        mock_dt.now.return_value = fixed_lk
        folder = make_job_folder(str(tmp_path), spec)
    # Folder name should contain LK timestamp
    assert "20260603_120000" in folder


# ── mail.send (C1/M2/M3) ──────────────────────────────────────────────────────

def test_mail_send_non_admin_forced_to_own_address():
    """A non-admin sender can only mail their OWN registered address (anti-relay)."""
    d, conn = _users_db()
    _insert_user(conn, "alice", role="tool", status="active", email="alice@iit.lk")
    sent = {}
    d._uid_to_username = lambda uid: "alice"
    d._uid_is_admin = lambda uid: False
    d._resend_send = lambda to, subject, html, bcc=None: (sent.update(to=to, bcc=bcc) or (True, "HTTP 200"))
    ok, data, err = d._h_mail_send(
        {"to": "attacker@evil.com", "subject": "x", "html": "y", "kind": "generic"},
        peer_uid=9000, users_conn=conn)
    assert ok, err
    assert sent["to"] == "alice@iit.lk"   # recipient overridden to self
    assert not sent["bcc"]                 # no relay BCC


def test_mail_send_login_dedup_skips_known_ip():
    d, conn = _users_db()
    _insert_user(conn, "bob", email="bob@iit.lk")
    conn.execute("UPDATE users SET last_seen_ip='1.2.3.4' WHERE username='bob'")
    conn.commit()
    d._uid_to_username = lambda uid: "bob"
    d._uid_is_admin = lambda uid: False
    calls = []
    d._resend_send = lambda *a, **k: calls.append(a) or (True, "ok")
    ok, data, _ = d._h_mail_send(
        {"kind": "login", "ip": "1.2.3.4", "subject": "s", "html": "h"},
        peer_uid=9000, users_conn=conn)
    assert ok
    assert data["sent"] is False          # known IP → not sent
    assert calls == []


def test_mail_send_login_sends_on_new_ip_and_updates():
    d, conn = _users_db()
    _insert_user(conn, "carol", email="carol@iit.lk")
    d._uid_to_username = lambda uid: "carol"
    d._uid_is_admin = lambda uid: False
    d._resend_send = lambda *a, **k: (True, "ok")
    ok, data, _ = d._h_mail_send(
        {"kind": "login", "ip": "9.9.9.9", "subject": "s", "html": "h"},
        peer_uid=9000, users_conn=conn)
    assert ok and data["sent"] is True
    row = conn.execute("SELECT last_seen_ip FROM users WHERE username='carol'").fetchone()
    assert row[0] == "9.9.9.9"            # IP recorded server-side


def test_mail_send_admin_never_auto_bccs_admins():
    """Admin/root senders must NOT auto-BCC other admins — user-facing mail
    (welcome, login, offboard) goes only to its recipient. Admins are notified
    via their own dedicated 'new user created' email instead."""
    d, conn = _users_db()
    _insert_user(conn, "admin1", role="admin", status="active", email="a1@iit.lk")
    _insert_user(conn, "admin2", role="admin", status="active", email="a2@iit.lk")
    sent = {}
    d._uid_is_admin = lambda uid: True
    d._uid_to_username = lambda uid: "admin1"
    d._resend_send = lambda to, subject, html, bcc=None: (sent.update(to=to, bcc=bcc) or (True, "ok"))
    ok, _, err = d._h_mail_send(
        {"to": "newuser@iit.lk", "subject": "welcome", "html": "h"},
        peer_uid=0, users_conn=conn)
    assert ok, err
    assert sent["to"] == "newuser@iit.lk"
    assert not sent["bcc"]                 # no admins silently copied


def test_mail_send_admin_honours_explicit_bcc():
    """An explicitly supplied bcc is still passed through for admin senders."""
    d, conn = _users_db()
    _insert_user(conn, "admin1", role="admin", status="active", email="a1@iit.lk")
    sent = {}
    d._uid_is_admin = lambda uid: True
    d._uid_to_username = lambda uid: "admin1"
    d._resend_send = lambda to, subject, html, bcc=None: (sent.update(to=to, bcc=bcc) or (True, "ok"))
    ok, _, err = d._h_mail_send(
        {"to": "x@iit.lk", "subject": "s", "html": "h", "bcc": ["ops@iit.lk"]},
        peer_uid=0, users_conn=conn)
    assert ok, err
    assert sent["bcc"] == ["ops@iit.lk"]


def test_mail_send_blocked_by_kill_switch(tmp_path, monkeypatch):
    """The admin kill-switch flag under NFS_ROOT makes the daemon report
    'not sent' without ever calling _resend_send."""
    d, conn = _users_db()
    _insert_user(conn, "admin1", role="admin", email="a1@iit.lk")
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / ".mail-disabled").write_text('{"disabled": true}')
    called = {"n": 0}
    d._uid_is_admin = lambda uid: True
    d._uid_to_username = lambda uid: "admin1"
    d._resend_send = lambda *a, **k: called.__setitem__("n", called["n"] + 1) or (True, "ok")
    ok, data, err = d._h_mail_send(
        {"to": "x@iit.lk", "subject": "s", "html": "h"}, peer_uid=0, users_conn=conn)
    assert ok and data.get("sent") is False
    assert called["n"] == 0, "kill-switch must short-circuit before sending"


def test_mail_send_non_admin_no_email_rejected():
    d, conn = _users_db()
    _insert_user(conn, "dave", email="")
    d._uid_to_username = lambda uid: "dave"
    d._uid_is_admin = lambda uid: False
    ok, data, err = d._h_mail_send(
        {"subject": "s", "html": "h"}, peer_uid=9000, users_conn=conn)
    assert not ok
    assert "no registered email" in err


def test_login_ip_dedup_cannot_be_preseeded_by_separate_verb():
    """M3: there must be no standalone verb to set last_seen_ip."""
    d = _load_daemon()
    assert not hasattr(d, "_h_users_update_login_ip"), \
        "standalone update_login_ip verb must be removed (M3)"
