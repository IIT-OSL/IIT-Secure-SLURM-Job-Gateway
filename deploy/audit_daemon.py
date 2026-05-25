# deploy/audit_daemon.py
"""
Audit daemon — runs as gpusync.
Receives events over a Unix datagram socket, drains the spool dir,
persists to SQLite (WAL) + JSONL, and periodically uploads audit.jsonl
to a Google Drive folder via service-account credentials.

Env vars:
  AUDIT_SOCKET   default /run/iit-gpu/audit.sock
  AUDIT_SPOOL    default /run/iit-gpu/spool
  AUDIT_STATE    default /var/lib/iit-gpu
  GDRIVE_FOLDER_ID               Google Drive folder ID to sync into (optional)
  GOOGLE_APPLICATION_CREDENTIALS path to service-account key JSON
"""
import json
import logging
import os
import select
import signal
import socket
import sqlite3
import time
import uuid
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
_log = logging.getLogger("audit_daemon")

SOCKET_PATH = os.environ.get("AUDIT_SOCKET", "/run/iit-gpu/audit.sock")
SPOOL_DIR = Path(os.environ.get("AUDIT_SPOOL", "/run/iit-gpu/spool"))
STATE_DIR = Path(os.environ.get("AUDIT_STATE", "/var/lib/iit-gpu"))
DB_PATH = STATE_DIR / "audit.db"
JSONL_PATH = STATE_DIR / "audit.jsonl"
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "")

_running = True


def _handle_signal(signum, frame) -> None:
    global _running
    _log.info(f"Signal {signum} received, shutting down.")
    _running = False


def _init_db(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            user    TEXT NOT NULL,
            session TEXT NOT NULL,
            action  TEXT NOT NULL,
            detail  TEXT,
            job_id  TEXT,
            remote  TEXT
        )
    """)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.commit()


def _insert(conn: sqlite3.Connection, event: dict) -> None:
    conn.execute(
        "INSERT INTO events (ts,user,session,action,detail,job_id,remote) VALUES (?,?,?,?,?,?,?)",
        (event.get("ts",""), event.get("user",""), event.get("session",""),
         event.get("action",""), event.get("detail",""), event.get("job_id",""), event.get("remote","")),
    )
    conn.commit()


def _append_jsonl(event: dict) -> None:
    with JSONL_PATH.open("a") as f:
        f.write(json.dumps(event) + "\n")


def _gdrive_sync() -> None:
    if not GDRIVE_FOLDER_ID or not JSONL_PATH.exists():
        return
    try:
        from google.oauth2.service_account import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
        creds = Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"],
            scopes=["https://www.googleapis.com/auth/drive.file"],
        )
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        results = svc.files().list(
            q=f"name='audit.jsonl' and '{GDRIVE_FOLDER_ID}' in parents and trashed=false",
            fields="files(id)",
        ).execute()
        files = results.get("files", [])
        media = MediaFileUpload(str(JSONL_PATH), mimetype="application/x-ndjson", resumable=False)
        if files:
            svc.files().update(fileId=files[0]["id"], media_body=media).execute()
        else:
            svc.files().create(
                body={"name": "audit.jsonl", "parents": [GDRIVE_FOLDER_ID]},
                media_body=media,
            ).execute()
    except Exception as exc:
        _log.warning(f"Google Drive sync failed (non-fatal): {exc}")


def _process(data: bytes, conn: sqlite3.Connection) -> None:
    try:
        event = json.loads(data.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning(f"Bad payload: {exc}")
        return
    _insert(conn, event)
    _append_jsonl(event)
    _log.info(f"user={event.get('user')} action={event.get('action')} job={event.get('job_id')}")


def _drain_spool(conn: sqlite3.Connection) -> None:
    if not SPOOL_DIR.exists():
        return
    for f in list(SPOOL_DIR.iterdir()):
        if f.suffix == ".json":
            try:
                _process(f.read_bytes(), conn)
                f.unlink()
            except OSError as exc:
                _log.warning(f"Spool drain error {f}: {exc}")


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    _init_db(conn)

    sock_path = Path(SOCKET_PATH)
    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(str(sock_path))
    sock_path.chmod(0o777)  # world-writable so any tool user can send
    sock.setblocking(False)

    _log.info(f"Listening on {SOCKET_PATH}")
    _drain_spool(conn)

    last_drain = time.monotonic()
    while _running:
        readable, _, _ = select.select([sock], [], [], 5.0)
        if readable:
            try:
                data, _ = sock.recvfrom(65535)
                _process(data, conn)
            except OSError:
                pass
        if time.monotonic() - last_drain > 30:
            _drain_spool(conn)
            last_drain = time.monotonic()

    sock.close()
    if sock_path.exists():
        sock_path.unlink()
    conn.close()
    _log.info("Daemon stopped.")


if __name__ == "__main__":
    main()
