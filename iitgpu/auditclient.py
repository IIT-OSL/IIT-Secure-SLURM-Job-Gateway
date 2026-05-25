# iitgpu/auditclient.py
import getpass
import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

AUDIT_SOCKET = os.environ.get("AUDIT_SOCKET", "/run/iit-gpu/audit.sock")
AUDIT_SPOOL = os.environ.get("AUDIT_SPOOL", "/run/iit-gpu/spool")

_SESSION_ID = str(uuid.uuid4())
_USER = getpass.getuser()
_REMOTE = (
    os.environ.get("SSH_CLIENT", "").split()[0]
    if os.environ.get("SSH_CLIENT")
    else "local"
)


def _build_event(action: str, detail: str, job_id: str) -> bytes:
    event = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "user": _USER,
        "session": _SESSION_ID,
        "action": action,
        "detail": detail,
        "job_id": job_id,
        "remote": _REMOTE,
    }
    return json.dumps(event).encode()


def _send_to_socket(data: bytes) -> bool:
    sock_path = os.environ.get("AUDIT_SOCKET", AUDIT_SOCKET)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(data, sock_path)
        return True
    except (OSError, AttributeError):
        # AttributeError: AF_UNIX not available on this platform (Windows dev)
        return False


def _spool(data: bytes) -> bool:
    spool_path = Path(os.environ.get("AUDIT_SPOOL", AUDIT_SPOOL))
    try:
        spool_path.mkdir(parents=True, exist_ok=True)
        (spool_path / f"{uuid.uuid4()}.json").write_bytes(data)
        return True
    except OSError:
        return False


def log(action: str, detail: str = "", job_id: str = "") -> bool:
    data = _build_event(action, detail, job_id)
    if _send_to_socket(data):
        return True
    return _spool(data)


def log_or_block(action: str, detail: str = "", job_id: str = "") -> bool:
    """Log the event; spool if socket unavailable. Returns False only if both socket and spool fail."""
    return log(action, detail, job_id)
