# deploy/audit_daemon.py
"""
Audit daemon — runs as gpusync.

Receives events over a Unix STREAM socket with SO_PEERCRED identity stamping,
drains the spool dir, and persists to SQLite (WAL) + JSONL.
Also brokers user-DB CRUD and privileged reads; all admin verbs are gated by
checking that the peer UID is in the admin group before execution.

Env vars:
  AUDIT_SOCKET   default /run/iit-gpu/audit.sock
  AUDIT_SPOOL    default /run/iit-gpu/spool
  AUDIT_STATE    default /var/lib/iit-gpu
  ADMIN_GROUP    default gpuadmins
  GPUUSERS_GROUP default gpuusers
  NFS_ROOT       default /shared
  JOBS_SUBDIR    default jobs
  MSMTP_LOG      default /var/log/msmtp.log
"""
import json
import logging
import os
import pwd
import select
import signal
import socket
import sqlite3
import struct
import time
import urllib.request
import urllib.error
from pathlib import Path

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("audit_daemon")

SOCKET_PATH    = os.environ.get("AUDIT_SOCKET",    "/run/iit-gpu/audit.sock")
SPOOL_DIR      = Path(os.environ.get("AUDIT_SPOOL",  "/run/iit-gpu/spool"))
STATE_DIR      = Path(os.environ.get("AUDIT_STATE",  "/var/lib/iit-gpu"))
DB_PATH        = STATE_DIR / "audit.db"
USERS_DB       = STATE_DIR / "users.db"
JSONL_PATH     = STATE_DIR / "audit.jsonl"
# Per-user hourly job_submit cap; 0 disables rate limiting.
JOB_RATE_LIMIT = int(os.environ.get("JOB_RATE_LIMIT", "20"))

# Secrets file holding RESEND_API_KEY — readable only by root + gpusync (0640
# root:gpusync). The daemon is the ONLY in-process mail sender, so regular users
# never need (and never get) read access to the API key. See C1 fix.
SECRETS_ENV    = os.environ.get("IIT_SECRETS_ENV", "/opt/iit-gpu/deploy/secrets.env")
_RESEND_URL    = "https://api.resend.com/emails"


def _load_secret(key: str) -> str:
    """Read a KEY=VALUE secret from SECRETS_ENV. Env var wins. Empty if absent."""
    if key in os.environ:
        return os.environ[key].strip()
    try:
        for raw in Path(SECRETS_ENV).read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == key:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _mail_from() -> str:
    return (_load_secret("MAIL_FROM")
            or os.environ.get("MAIL_FROM", "GPU Cluster <no-reply@example.com>"))


def _resend_send(to: str, subject: str, html: str,
                 bcc: list | None = None) -> tuple[bool, str]:
    """Send one email via the Resend HTTP API. Key never leaves the daemon."""
    key = _load_secret("RESEND_API_KEY")
    if not key:
        return False, "RESEND_API_KEY not configured on the daemon"
    payload = {"from": _mail_from(), "to": [to], "subject": subject, "html": html}
    if bcc:
        payload["bcc"] = bcc
    req = urllib.request.Request(
        _RESEND_URL, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "User-Agent": "iit-gpu-mailer/1.0",
                 "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return (200 <= resp.status < 300), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}: {exc.reason}"
    except Exception as exc:
        return False, str(exc)

_running = True
_MAX_MSG  = 1_048_576   # 1 MiB hard cap on any single message

_ALLOWED_UNITS = frozenset({
    "iit-gpu-audit", "slurmctld", "slurmd", "mariadb", "slurmdbd",
})


# ─── signal handling ──────────────────────────────────────────────────────────

def _handle_signal(signum, frame):
    global _running
    _log.info("Signal %s received, shutting down.", signum)
    _running = False


# ─── schema ───────────────────────────────────────────────────────────────────

def _init_audit_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL,
            user     TEXT NOT NULL,
            session  TEXT NOT NULL,
            action   TEXT NOT NULL,
            detail   TEXT,
            job_id   TEXT,
            remote   TEXT,
            meta     TEXT,
            identity TEXT
        )
    """)
    for col, typedef in [("meta", "TEXT"), ("identity", "TEXT")]:
        try:
            conn.execute(f"ALTER TABLE events ADD COLUMN {col} {typedef}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_user   ON events(user)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_action ON events(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_events_ts     ON events(ts)")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()


def _init_users_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            username      TEXT PRIMARY KEY,
            uid           INTEGER,
            full_name     TEXT,
            email         TEXT NOT NULL,
            role          TEXT NOT NULL CHECK (role IN ('admin','tool','shell')),
            status        TEXT NOT NULL DEFAULT 'active'
                          CHECK (status IN ('active','offboarded')),
            created_at    TEXT NOT NULL,
            created_by    TEXT NOT NULL,
            notes         TEXT,
            must_change_pw INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")
    conn.execute("PRAGMA journal_mode=WAL")
    # Migrate existing DBs — add columns introduced after initial schema.
    for migration in (
        "ALTER TABLE users ADD COLUMN must_change_pw INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE users ADD COLUMN last_seen_ip TEXT NOT NULL DEFAULT ''",
    ):
        try:
            conn.execute(migration)
            conn.commit()
        except Exception:
            pass  # column already exists
    conn.commit()


# ─── audit DB helpers ─────────────────────────────────────────────────────────

def _insert(conn: sqlite3.Connection, event: dict) -> None:
    meta = event.get("meta")
    if meta is not None and not isinstance(meta, str):
        meta = json.dumps(meta)
    conn.execute(
        "INSERT INTO events "
        "(ts,user,session,action,detail,job_id,remote,meta,identity) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (event.get("ts", ""), event.get("user", ""),
         event.get("session", ""), event.get("action", ""),
         event.get("detail", ""), event.get("job_id", ""),
         event.get("remote", ""), meta,
         event.get("identity", "peercred")),
    )
    conn.commit()


def _append_jsonl(event: dict) -> None:
    with JSONL_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _self_audit(audit_conn: sqlite3.Connection, username: str,
                action: str, detail: str = "",
                meta: dict | None = None) -> None:
    """Daemon-internal audit record (identity='daemon')."""
    from datetime import datetime, timezone
    event = {
        "ts":       datetime.now(timezone.utc).isoformat(),
        "user":     username,
        "session":  "daemon",
        "action":   action,
        "detail":   detail,
        "job_id":   "",
        "remote":   "",
        "meta":     json.dumps(meta) if meta else None,
        "identity": "daemon",
    }
    try:
        _insert(audit_conn, event)
        _append_jsonl(event)
    except Exception as exc:
        _log.warning("Internal audit log failed: %s", exc)


# ─── SO_PEERCRED / identity ───────────────────────────────────────────────────

def _get_peer_uid(sock: socket.socket) -> int | None:
    try:
        creds = sock.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    except (OSError, struct.error):
        return None


def _uid_to_username(uid: int) -> str | None:
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


def _uid_is_admin(uid: int) -> bool:
    import grp
    if uid == 0:          # root always has admin access to the daemon
        return True
    admin_group = os.environ.get("ADMIN_GROUP", "gpuadmins")
    try:
        pw = pwd.getpwuid(uid)
        g  = grp.getgrnam(admin_group)
        return pw.pw_name in g.gr_mem or pw.pw_gid == g.gr_gid
    except (KeyError, OSError):
        return False


# ─── stream framing ───────────────────────────────────────────────────────────

def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


def _read_message(sock: socket.socket) -> bytes | None:
    raw_len = _recv_exactly(sock, 4)
    if raw_len is None:
        return None
    length = struct.unpack(">I", raw_len)[0]
    if length > _MAX_MSG:
        _log.warning("Oversized message (%d bytes), dropping", length)
        return None
    return _recv_exactly(sock, length)


def _send_response(sock: socket.socket, ok: bool,
                   data=None, error: str = "") -> None:
    resp: dict = {"ok": ok}
    if data is not None:
        resp["data"] = data
    if error:
        resp["error"] = error
    try:
        body = json.dumps(resp).encode()
        sock.sendall(struct.pack(">I", len(body)) + body)
    except OSError:
        pass   # client may have already closed (audit.log callers don't read response)


# ─── verb handlers ────────────────────────────────────────────────────────────

def _count_recent_submissions(conn: sqlite3.Connection,
                               user: str, window_seconds: int = 3600) -> int:
    """Count job_submit events for user in the last window_seconds."""
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM events WHERE user=? AND action='job_submit' AND ts>=?",
        (user, cutoff),
    ).fetchone()
    return row[0] if row else 0


def _h_audit_log(payload: dict, peer_username: str | None,
                 audit_conn: sqlite3.Connection):
    event = dict(payload)
    if peer_username:
        event["user"]     = peer_username
        event["identity"] = "peercred"
    else:
        event.setdefault("identity", "spooled")

    # Rate-limit job_submit events from live socket connections.
    if (event.get("action") == "job_submit"
            and peer_username is not None
            and JOB_RATE_LIMIT > 0):
        count = _count_recent_submissions(audit_conn, peer_username)
        if count >= JOB_RATE_LIMIT:
            _log.warning("rate limit: user=%s blocked (count=%d limit=%d)",
                         peer_username, count, JOB_RATE_LIMIT)
            return False, None, f"rate limit: {count} job_submit events in last hour"

    _insert(audit_conn, event)
    _append_jsonl(event)
    _log.info("user=%s action=%s job=%s",
              event.get("user"), event.get("action"), event.get("job_id"))
    return True, None, ""


def _h_users_create(payload: dict, peer_uid: int,
                    users_conn: sqlite3.Connection,
                    audit_conn: sqlite3.Connection):
    from datetime import datetime, timezone
    peer_user = _uid_to_username(peer_uid) or str(peer_uid)
    username  = payload.get("username", "").strip()
    email     = payload.get("email", "").strip()
    role      = payload.get("role", "")
    if not username or not email or role not in ("admin", "tool", "shell"):
        return False, None, "username, email, and role (admin/tool/shell) required"
    try:
        uid_val = pwd.getpwnam(username).pw_uid
    except KeyError:
        uid_val = None
    now            = datetime.now(timezone.utc).isoformat()
    must_change_pw = 1 if payload.get("must_change_pw") else 0
    full_name      = payload.get("full_name", "")
    notes          = payload.get("notes", "")
    existing = users_conn.execute(
        "SELECT status FROM users WHERE username=?", (username,)
    ).fetchone()
    try:
        if existing:
            if existing[0] != "offboarded":
                return False, None, f"user '{username}' already exists and is active"
            users_conn.execute(
                "UPDATE users SET uid=?,full_name=?,email=?,role=?,status='active',"
                "created_at=?,created_by=?,notes=?,must_change_pw=? WHERE username=?",
                (uid_val, full_name, email, role,
                 now, peer_user, notes, must_change_pw, username),
            )
        else:
            users_conn.execute(
                "INSERT INTO users "
                "(username,uid,full_name,email,role,status,created_at,created_by,notes,must_change_pw) "
                "VALUES (?,?,?,?,?,'active',?,?,?,?)",
                (username, uid_val, full_name, email, role,
                 now, peer_user, notes, must_change_pw),
            )
        users_conn.commit()
    except sqlite3.IntegrityError as exc:
        return False, None, str(exc)
    _self_audit(audit_conn, peer_user, "admin_provision_user",
                detail=username, meta={"role": role, "email": email,
                                       "must_change_pw": bool(must_change_pw)})
    return True, {"username": username}, ""


def _h_users_get(payload: dict, users_conn: sqlite3.Connection):
    username = payload.get("username", "").strip()
    row = users_conn.execute(
        "SELECT username,uid,full_name,email,role,status,"
        "created_at,created_by,notes FROM users WHERE username=?",
        (username,)
    ).fetchone()
    if not row:
        return False, None, "user not found"
    cols = ("username", "uid", "full_name", "email", "role", "status",
            "created_at", "created_by", "notes")
    return True, dict(zip(cols, row)), ""


def _h_users_list(users_conn: sqlite3.Connection):
    rows = users_conn.execute(
        "SELECT username,uid,full_name,email,role,status,created_at,created_by "
        "FROM users ORDER BY username"
    ).fetchall()
    cols = ("username", "uid", "full_name", "email", "role", "status",
            "created_at", "created_by")
    return True, {"users": [dict(zip(cols, r)) for r in rows]}, ""


def _h_users_offboard(payload: dict, peer_uid: int,
                      users_conn: sqlite3.Connection,
                      audit_conn: sqlite3.Connection):
    peer_user = _uid_to_username(peer_uid) or str(peer_uid)
    username  = payload.get("username", "").strip()
    if not username:
        return False, None, "username required"
    cur = users_conn.execute(
        "UPDATE users SET status='offboarded' "
        "WHERE username=? AND status='active'", (username,))
    users_conn.commit()
    if cur.rowcount == 0:
        return False, None, "user not found or already offboarded"
    _self_audit(audit_conn, peer_user, "admin_offboard_user", detail=username)
    return True, {"username": username}, ""


def _h_users_reconcile(users_conn: sqlite3.Connection):
    import grp
    gpuusers = os.environ.get("GPUUSERS_GROUP", "gpuusers")
    admins   = os.environ.get("ADMIN_GROUP",    "gpuadmins")
    rows     = users_conn.execute(
        "SELECT username FROM users WHERE status='active'"
    ).fetchall()
    db_users: set[str] = {r[0] for r in rows}
    os_users: set[str] = set()
    for grp_name in (gpuusers, admins):
        try:
            os_users.update(grp.getgrnam(grp_name).gr_mem)
        except KeyError:
            pass
    return True, {
        "db_only": sorted(db_users - os_users),
        "os_only": sorted(os_users - db_users),
    }, ""


def _h_users_email_for(payload: dict, peer_uid: int,
                       users_conn: sqlite3.Connection):
    username     = payload.get("username", "").strip()
    peer_user    = _uid_to_username(peer_uid) or ""
    if not username:
        return False, None, "username required"
    if peer_user != username and not _uid_is_admin(peer_uid):
        return False, None, "permission denied: can only look up your own email"
    row = users_conn.execute(
        "SELECT email FROM users WHERE username=? AND status='active'",
        (username,)
    ).fetchone()
    if not row:
        return False, None, "user not found"
    return True, {"email": row[0]}, ""


def _h_audit_query(payload: dict, peer_uid: int,
                   audit_conn: sqlite3.Connection):
    peer_user    = _uid_to_username(peer_uid) or str(peer_uid)
    user_filter  = payload.get("user", "")
    action_filter= payload.get("action", "")
    date_from    = payload.get("date_from", "")
    date_to      = payload.get("date_to", "")
    limit        = min(int(payload.get("limit", 100)), 500)

    clauses, params = [], []
    if user_filter:
        clauses.append("user=?");       params.append(user_filter)
    if action_filter:
        clauses.append("action LIKE ?"); params.append(f"%{action_filter}%")
    if date_from:
        clauses.append("ts>=?");        params.append(date_from)
    if date_to:
        clauses.append("ts<=?");        params.append(date_to)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(limit)
    rows = audit_conn.execute(
        f"SELECT ts,user,session,action,detail,job_id,remote,meta,identity "
        f"FROM events {where} ORDER BY ts DESC LIMIT ?", params
    ).fetchall()
    cols = ("ts","user","session","action","detail","job_id","remote","meta","identity")
    events = []
    for r in rows:
        ev = dict(zip(cols, r))
        if ev.get("meta"):
            try:
                ev["meta"] = json.loads(ev["meta"])
            except (ValueError, TypeError):
                pass
        events.append(ev)
    _self_audit(audit_conn, peer_user, "admin_viewed_audit",
                detail=f"user={user_filter} action={action_filter}")
    return True, {"events": events}, ""


def _h_roster_view(peer_uid: int, users_conn: sqlite3.Connection,
                   audit_conn: sqlite3.Connection):
    peer_user    = _uid_to_username(peer_uid) or str(peer_uid)
    _, udata, _  = _h_users_list(users_conn)
    _, rdata, _  = _h_users_reconcile(users_conn)
    _self_audit(audit_conn, peer_user, "admin_viewed_roster")
    return True, {
        "users": (udata or {}).get("users", []),
        "drift": rdata or {},
    }, ""


def _h_users_admin_emails(users_conn: sqlite3.Connection):
    rows = users_conn.execute(
        "SELECT email FROM users WHERE role='admin' AND status='active' AND email != ''"
    ).fetchall()
    return True, {"emails": [r[0] for r in rows]}, ""


def _h_mail_send(payload: dict, peer_uid: int,
                 users_conn: sqlite3.Connection):
    """Send transactional mail. The daemon holds the API key (C1 fix).

    Trust model:
    - admin / root callers: may send to any recipient; admin BCC is added
      automatically when no explicit bcc is given.
    - non-admin callers: recipient is FORCED to their own registered address and
      BCC is stripped — this prevents both API-key theft and using the daemon as
      an open relay to spoof mail from the cluster's verified domain.
    - kind='login': the daemon does the new-IP dedup itself and only sends when
      the source IP is unseen (M3 fix — no standalone IP-seeding verb).
    """
    to      = (payload.get("to") or "").strip()
    subject = payload.get("subject") or ""
    html    = payload.get("html") or ""
    bcc     = payload.get("bcc") or []
    kind    = payload.get("kind") or "generic"
    ip      = (payload.get("ip") or "").strip()
    is_admin = _uid_is_admin(peer_uid) or peer_uid == 0
    peer_user = _uid_to_username(peer_uid) or ""

    if not is_admin:
        row = users_conn.execute(
            "SELECT email, last_seen_ip FROM users "
            "WHERE username=? AND status='active'", (peer_user,)
        ).fetchone()
        if not row or not row[0]:
            return False, None, "no registered email for sender"
        to  = row[0]            # force recipient to self
        bcc = []                # no relay
        if kind == "login":
            if row[1] == ip:
                return True, {"sent": False, "reason": "known ip"}, ""
            users_conn.execute(
                "UPDATE users SET last_seen_ip=? WHERE username=?", (ip, peer_user))
            users_conn.commit()
    else:
        if not to:
            return False, None, "recipient required"
        if not bcc:
            rows = users_conn.execute(
                "SELECT email FROM users WHERE role='admin' AND status='active' "
                "AND email != ''").fetchall()
            bcc = [r[0] for r in rows if r[0] != to]

    ok_send, msg = _resend_send(to, subject, html, bcc or None)
    if ok_send:
        return True, {"sent": True}, ""
    return False, None, msg


def _h_users_check_must_change_pw(payload: dict, peer_uid: int,
                                   users_conn: sqlite3.Connection):
    """Accessible to the user themselves or any admin."""
    username  = payload.get("username", "").strip()
    peer_user = _uid_to_username(peer_uid) or ""
    if not username:
        return False, None, "username required"
    if peer_user != username and not _uid_is_admin(peer_uid):
        return False, None, "permission denied"
    row = users_conn.execute(
        "SELECT must_change_pw FROM users WHERE username=? AND status='active'",
        (username,)
    ).fetchone()
    if not row:
        return False, None, "user not found"
    return True, {"must_change_pw": bool(row[0])}, ""


def _h_users_clear_must_change_pw(payload: dict, peer_uid: int,
                                   users_conn: sqlite3.Connection):
    """User clears their own flag after changing password."""
    username  = payload.get("username", "").strip()
    peer_user = _uid_to_username(peer_uid) or ""
    if not username:
        return False, None, "username required"
    if peer_user != username and not _uid_is_admin(peer_uid):
        return False, None, "permission denied"
    users_conn.execute(
        "UPDATE users SET must_change_pw=0 WHERE username=?", (username,)
    )
    users_conn.commit()
    return True, {"username": username}, ""


def _h_maillog_tail(payload: dict, peer_uid: int,
                    audit_conn: sqlite3.Connection):
    peer_user = _uid_to_username(peer_uid) or str(peer_uid)
    n         = min(int(payload.get("lines", 50)), 200)
    log_path  = os.environ.get("MSMTP_LOG", "/var/log/msmtp.log")
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()[-n:]
    except OSError as exc:
        return False, None, str(exc)
    _self_audit(audit_conn, peer_user, "admin_viewed_maillog")
    return True, {"lines": lines}, ""


def _h_joblog_read(payload: dict, peer_uid: int,
                   audit_conn: sqlite3.Connection):
    peer_user   = _uid_to_username(peer_uid) or str(peer_uid)
    target_user = payload.get("user", "").strip()
    filename    = payload.get("filename", "").strip()
    if not target_user or not filename:
        return False, None, "user and filename required"
    nfs_root    = os.environ.get("NFS_ROOT", "/shared")
    jobs_sub    = os.environ.get("JOBS_SUBDIR", "jobs")
    allowed     = Path(nfs_root) / jobs_sub / target_user
    try:
        resolved = (allowed / filename).resolve()
    except OSError:
        return False, None, "invalid path"
    if not str(resolved).startswith(str(allowed.resolve())):
        return False, None, "path outside allowed directory"
    if not filename.endswith((".out", ".err")):
        return False, None, "only .out and .err files allowed"
    try:
        content = resolved.read_text(errors="replace")
    except OSError as exc:
        return False, None, str(exc)
    _self_audit(audit_conn, peer_user, "admin_read_user_log",
                detail=target_user, meta={"filename": filename})
    return True, {"content": content, "path": str(resolved)}, ""


def _h_service_status(payload: dict, peer_uid: int,
                      audit_conn: sqlite3.Connection):
    import subprocess
    peer_user = _uid_to_username(peer_uid) or str(peer_uid)
    unit      = payload.get("unit", "").strip()
    if unit not in _ALLOWED_UNITS:
        return False, None, f"unit not in allowlist: {sorted(_ALLOWED_UNITS)}"
    try:
        active = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()
        journal = subprocess.run(
            ["journalctl", "-u", unit, "-n", "20", "--no-pager"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, None, str(exc)
    _self_audit(audit_conn, peer_user, "admin_viewed_service", detail=unit)
    return True, {"unit": unit, "active": active, "journal": journal}, ""


# ─── dispatch ─────────────────────────────────────────────────────────────────

_ADMIN_VERBS = frozenset({
    "users.create", "users.get", "users.list", "users.offboard",
    "users.reconcile", "audit.query", "roster.view", "maillog.tail",
    "joblog.read", "service.status",
})


def _dispatch(verb: str, payload: dict, peer_uid: int | None,
              peer_username: str | None,
              audit_conn: sqlite3.Connection,
              users_conn: sqlite3.Connection):

    if verb == "audit.log":
        return _h_audit_log(payload, peer_username, audit_conn)

    if peer_uid is None:
        return False, None, "cannot determine peer identity"

    if verb == "users.email_for":
        return _h_users_email_for(payload, peer_uid, users_conn)

    if verb == "mail.send":
        # Any local identity may request a send; the handler itself enforces
        # self-only recipients for non-admins (anti-relay) and holds the key.
        return _h_mail_send(payload, peer_uid, users_conn)

    if verb == "users.admin_emails":
        # M2: admin emails are PII — restrict to admins and root (the SLURM
        # mailer runs as root). No longer readable by every gpuusers member.
        if not (_uid_is_admin(peer_uid) or peer_uid == 0):
            return False, None, "permission denied: admin or root required"
        return _h_users_admin_emails(users_conn)

    if verb == "users.check_must_change_pw":
        return _h_users_check_must_change_pw(payload, peer_uid, users_conn)

    if verb == "users.clear_must_change_pw":
        return _h_users_clear_must_change_pw(payload, peer_uid, users_conn)

    if verb in _ADMIN_VERBS:
        if not _uid_is_admin(peer_uid):
            return False, None, "permission denied: admin group required"
        if verb == "users.create":
            return _h_users_create(payload, peer_uid, users_conn, audit_conn)
        if verb == "users.get":
            return _h_users_get(payload, users_conn)
        if verb == "users.list":
            return _h_users_list(users_conn)
        if verb == "users.offboard":
            return _h_users_offboard(payload, peer_uid, users_conn, audit_conn)
        if verb == "users.reconcile":
            return _h_users_reconcile(users_conn)
        if verb == "audit.query":
            return _h_audit_query(payload, peer_uid, audit_conn)
        if verb == "roster.view":
            return _h_roster_view(peer_uid, users_conn, audit_conn)
        if verb == "maillog.tail":
            return _h_maillog_tail(payload, peer_uid, audit_conn)
        if verb == "joblog.read":
            return _h_joblog_read(payload, peer_uid, audit_conn)
        if verb == "service.status":
            return _h_service_status(payload, peer_uid, audit_conn)

    return False, None, f"unknown verb: {verb}"


# ─── connection handler ───────────────────────────────────────────────────────

def _handle_connection(conn_sock: socket.socket,
                       audit_conn: sqlite3.Connection,
                       users_conn: sqlite3.Connection) -> None:
    try:
        conn_sock.settimeout(5.0)
        peer_uid      = _get_peer_uid(conn_sock)
        peer_username = _uid_to_username(peer_uid) if peer_uid is not None else None

        data = _read_message(conn_sock)
        if data is None:
            return

        try:
            req = json.loads(data.decode())
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            _send_response(conn_sock, False, error=f"bad JSON: {exc}")
            return

        verb    = req.get("verb", "")
        payload = req.get("payload", {})
        ok, result, err = _dispatch(verb, payload, peer_uid,
                                     peer_username, audit_conn, users_conn)
        _send_response(conn_sock, ok, result, err)

    except OSError as exc:
        _log.debug("Connection error: %s", exc)
    finally:
        try:
            conn_sock.close()
        except OSError:
            pass


# ─── spool ────────────────────────────────────────────────────────────────────

def _process_event(data: bytes, conn: sqlite3.Connection,
                   peer_username: str | None = None) -> None:
    try:
        event = json.loads(data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("Bad spool payload: %s", exc)
        return
    if peer_username:
        event["user"]     = peer_username
        event["identity"] = "peercred"
    else:
        event.setdefault("identity", "spooled")
    _insert(conn, event)
    _append_jsonl(event)
    _log.info("spool user=%s action=%s", event.get("user"), event.get("action"))


def _drain_spool(conn: sqlite3.Connection) -> None:
    if not SPOOL_DIR.exists():
        return
    for f in list(SPOOL_DIR.iterdir()):
        if f.suffix == ".json":
            try:
                _process_event(f.read_bytes(), conn)
                f.unlink()
            except OSError as exc:
                _log.warning("Spool drain error %s: %s", f, exc)


# ─── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    audit_conn = sqlite3.connect(str(DB_PATH))
    _init_audit_db(audit_conn)

    users_conn = sqlite3.connect(str(USERS_DB))
    _init_users_db(users_conn)
    try:
        USERS_DB.chmod(0o600)
    except OSError:
        pass

    sock_path = Path(SOCKET_PATH)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    sock_path.chmod(0o777)   # world-connectable; SO_PEERCRED is the security boundary
    server.listen(16)
    server.setblocking(False)

    _log.info("Listening on %s (STREAM, SO_PEERCRED)", SOCKET_PATH)
    _drain_spool(audit_conn)

    last_drain = time.monotonic()
    while _running:
        readable, _, _ = select.select([server], [], [], 5.0)
        for s in readable:
            if s is server:
                try:
                    conn_sock, _ = server.accept()
                    _handle_connection(conn_sock, audit_conn, users_conn)
                except OSError:
                    pass
        if time.monotonic() - last_drain > 30:
            _drain_spool(audit_conn)
            last_drain = time.monotonic()

    server.close()
    if sock_path.exists():
        sock_path.unlink()
    audit_conn.close()
    users_conn.close()
    _log.info("Daemon stopped.")


if __name__ == "__main__":
    main()
