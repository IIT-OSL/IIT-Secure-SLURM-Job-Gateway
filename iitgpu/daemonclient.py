# iitgpu/daemonclient.py
"""Typed wrappers around daemon_request for all broker verbs.

The TUI and admin panel use these functions rather than calling daemon_request
directly, so the wire protocol is encapsulated here.
"""
from __future__ import annotations

from iitgpu.auditclient import daemon_request


def email_for(username: str) -> str | None:
    """Return the email address for a username (self or admin).  None on failure."""
    resp = daemon_request("users.email_for", {"username": username})
    if resp.get("ok"):
        return resp.get("data", {}).get("email")
    return None


def create_user(username: str, email: str, role: str,
                full_name: str = "", notes: str = "") -> tuple[bool, str]:
    resp = daemon_request("users.create", {
        "username":  username,
        "email":     email,
        "role":      role,
        "full_name": full_name,
        "notes":     notes,
    })
    if resp.get("ok"):
        return True, f"user record created for {username}"
    return False, resp.get("error", "daemon error")


def get_user(username: str) -> dict | None:
    resp = daemon_request("users.get", {"username": username})
    return resp.get("data") if resp.get("ok") else None


def list_users() -> list[dict]:
    resp = daemon_request("users.list", {})
    return resp.get("data", {}).get("users", []) if resp.get("ok") else []


def offboard_user(username: str) -> tuple[bool, str]:
    resp = daemon_request("users.offboard", {"username": username})
    if resp.get("ok"):
        return True, f"{username} offboarded in user DB"
    return False, resp.get("error", "daemon error")


def reconcile() -> dict:
    resp = daemon_request("users.reconcile", {})
    return resp.get("data", {}) if resp.get("ok") else {}


def query_audit(user: str = "", action: str = "",
                date_from: str = "", date_to: str = "",
                limit: int = 100) -> list[dict]:
    resp = daemon_request("audit.query", {
        "user": user, "action": action,
        "date_from": date_from, "date_to": date_to,
        "limit": limit,
    })
    return resp.get("data", {}).get("events", []) if resp.get("ok") else []


def view_roster() -> dict:
    resp = daemon_request("roster.view", {})
    return resp.get("data", {}) if resp.get("ok") else {}


def tail_maillog(lines: int = 50) -> list[str]:
    resp = daemon_request("maillog.tail", {"lines": lines})
    return resp.get("data", {}).get("lines", []) if resp.get("ok") else []


def read_job_log(user: str, filename: str) -> tuple[bool, str]:
    resp = daemon_request("joblog.read", {"user": user, "filename": filename})
    if resp.get("ok"):
        return True, resp["data"]["content"]
    return False, resp.get("error", "daemon error")


def service_status(unit: str) -> tuple[bool, dict]:
    resp = daemon_request("service.status", {"unit": unit})
    if resp.get("ok"):
        return True, resp.get("data", {})
    return False, {"error": resp.get("error", "daemon error")}


def admin_emails() -> list[str]:
    """Return email addresses of all active admin-role users (for BCC). Best-effort."""
    resp = daemon_request("users.admin_emails", {})
    return resp.get("data", {}).get("emails", []) if resp.get("ok") else []
