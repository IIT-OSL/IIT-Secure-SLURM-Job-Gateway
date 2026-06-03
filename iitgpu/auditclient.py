# iitgpu/auditclient.py
"""
Audit client — sends events to the daemon over a Unix STREAM socket.

The daemon resolves the authoritative username via SO_PEERCRED; the _USER
field is sent for spool-fallback bookkeeping only and is overridden by the
daemon on ingestion from the socket (not from spool files).
"""
from __future__ import annotations

import getpass
import json
import os
import socket
import struct
import uuid
from datetime import datetime, timezone
from pathlib import Path

AUDIT_SOCKET = os.environ.get("AUDIT_SOCKET", "/run/iit-gpu/audit.sock")
AUDIT_SPOOL  = os.environ.get("AUDIT_SPOOL",  "/run/iit-gpu/spool")

_SESSION_ID  = str(uuid.uuid4())
_USER        = getpass.getuser()
_REMOTE      = (
    os.environ.get("SSH_CLIENT", "").split()[0]
    if os.environ.get("SSH_CLIENT") else "local"
)

_CONN_TIMEOUT = 2.0


# ─── internal helpers ─────────────────────────────────────────────────────────

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


def _send_framed(sock: socket.socket, data: bytes) -> None:
    sock.sendall(struct.pack(">I", len(data)) + data)


# ─── event building ───────────────────────────────────────────────────────────

def _build_event(action: str, detail: str, job_id: str,
                 meta: dict | None = None) -> dict:
    ev: dict = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "user":    _USER,
        "session": _SESSION_ID,
        "action":  action,
        "detail":  detail,
        "job_id":  job_id,
        "remote":  _REMOTE,
    }
    if meta is not None:
        ev["meta"] = meta
    return ev


# ─── transport ────────────────────────────────────────────────────────────────

def _send_to_socket(event: dict) -> bool:
    """Send an audit.log request to the daemon. No response is read."""
    req  = {"verb": "audit.log", "payload": event}
    data = json.dumps(req).encode()
    sock_path = os.environ.get("AUDIT_SOCKET", AUDIT_SOCKET)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_CONN_TIMEOUT)
            sock.connect(sock_path)
            _send_framed(sock, data)
        return True
    except (OSError, AttributeError):
        return False


def _spool(event: dict) -> bool:
    """Write event to spool dir when socket is unavailable."""
    spooled_event = {**event, "identity": "spooled"}
    data = json.dumps(spooled_event).encode()
    spool_path = Path(os.environ.get("AUDIT_SPOOL", AUDIT_SPOOL))
    try:
        spool_path.mkdir(parents=True, exist_ok=True)
        (spool_path / f"{uuid.uuid4()}.json").write_bytes(data)
        return True
    except OSError:
        return False


# ─── public API ───────────────────────────────────────────────────────────────

def log(action: str, detail: str = "", job_id: str = "",
        meta: dict | None = None) -> bool:
    event = _build_event(action, detail, job_id, meta)
    if _send_to_socket(event):
        return True
    return _spool(event)


def log_or_block(action: str, detail: str = "", job_id: str = "",
                 meta: dict | None = None) -> bool:
    """Log the event and check whether the daemon blocks it.

    Returns False if the daemon explicitly blocks the action (e.g. rate limit
    exceeded) or if the event cannot be delivered at all.  Always returns True
    when the daemon accepts the event.  Falls back to spool-and-True when the
    daemon is unreachable (fail-open for non-blocking events).
    """
    event = _build_event(action, detail, job_id, meta)
    resp  = daemon_request("audit.log", event)
    if resp.get("ok"):
        return True
    # Daemon explicitly blocked — do not spool, do not allow.
    if resp.get("error", "").startswith("rate limit"):
        return False
    # Daemon unreachable: spool the event and allow (fail-open).
    _spool(event)
    return True


def daemon_request(verb: str, payload: dict, timeout: float | None = None) -> dict:
    """Send a request to the daemon and return its JSON response dict.

    Returns {"ok": False, "error": "..."} on any connection or decode failure.
    `timeout` overrides the default short socket timeout — needed for verbs like
    mail.send where the daemon performs an outbound HTTP call before replying.
    """
    req  = {"verb": verb, "payload": payload}
    data = json.dumps(req).encode()
    sock_path = os.environ.get("AUDIT_SOCKET", AUDIT_SOCKET)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout if timeout is not None else _CONN_TIMEOUT)
            sock.connect(sock_path)
            _send_framed(sock, data)
            raw_len = _recv_exactly(sock, 4)
            if not raw_len:
                return {"ok": False, "error": "no response from daemon"}
            length = struct.unpack(">I", raw_len)[0]
            if length > 4 * 1024 * 1024:
                return {"ok": False, "error": "response too large"}
            resp_data = _recv_exactly(sock, length)
            if resp_data is None:
                return {"ok": False, "error": "incomplete response"}
            return json.loads(resp_data.decode())
    except (OSError, json.JSONDecodeError, struct.error, AttributeError) as exc:
        return {"ok": False, "error": str(exc)}
