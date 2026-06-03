# tests/test_mailer.py
"""Phase 1 — credential-safety tests for iitgpu/mailer.py."""
from unittest.mock import patch, call
import pytest
from iitgpu import mailer


# ── send_welcome ──────────────────────────────────────────────────────────────

def test_welcome_sends_to_correct_recipient():
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured.update(to=to, subject=subject, html=html, bcc=bcc)
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=["admin@iit.lk"]):
        mailer.send_welcome("alice", "alice@iit.lk", "Alice Smith")
        import threading, time; time.sleep(0.05)
    assert captured["to"] == "alice@iit.lk"


def test_welcome_subject_contains_username():
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured["subject"] = subject
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=[]):
        mailer.send_welcome("alice", "alice@iit.lk")
        import time; time.sleep(0.05)
    assert "alice" in captured["subject"]


def test_welcome_html_contains_no_password():
    """The welcome email must never contain a password field."""
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured["html"] = html
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=[]):
        mailer.send_welcome("alice", "alice@iit.lk", "Alice")
        import time; time.sleep(0.05)
    html = captured.get("html", "")
    # Must not expose any password-like content
    assert "Password" not in html or "change" in html.lower(), \
        "welcome email must not contain a Password credential row"
    # Confirm the password row label is absent (only 'change' language is allowed)
    assert ">Password<" not in html


def test_welcome_has_no_bcc():
    """Welcome email must not BCC admins — it must be private to the recipient."""
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured["bcc"] = bcc
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=["admin@iit.lk", "boss@iit.lk"]):
        mailer.send_welcome("alice", "alice@iit.lk")
        import time; time.sleep(0.05)
    assert not captured.get("bcc"), \
        f"welcome email BCC must be empty/None, got: {captured.get('bcc')}"


def test_welcome_contains_username_and_ssh_command():
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured["html"] = html
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=[]):
        mailer.send_welcome("bob", "bob@iit.lk")
        import time; time.sleep(0.05)
    html = captured.get("html", "")
    assert "bob" in html
    assert "ssh" in html.lower()


def test_welcome_signature_accepts_no_password_param():
    """send_welcome must not accept a password positional argument."""
    import inspect
    sig = inspect.signature(mailer.send_welcome)
    param_names = list(sig.parameters.keys())
    assert "password" not in param_names, \
        f"send_welcome still has a 'password' parameter: {param_names}"


# ── other emails still BCC admins ─────────────────────────────────────────────

def test_login_notification_bccs_admins():
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured["bcc"] = bcc
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=["admin@iit.lk"]):
        mailer.send_login_notification("alice", "alice@iit.lk", "10.35.4.5")
        import time; time.sleep(0.05)
    assert "admin@iit.lk" in (captured.get("bcc") or [])


def test_offboard_bccs_admins():
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured["bcc"] = bcc
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=["admin@iit.lk"]):
        mailer.send_offboard("alice", "alice@iit.lk", "Alice Smith")
        import time; time.sleep(0.05)
    assert "admin@iit.lk" in (captured.get("bcc") or [])


def test_recipient_excluded_from_own_bcc_on_login():
    captured = {}
    def fake_send(to, subject, html, bcc=None):
        captured.update(to=to, bcc=bcc)
    with patch("iitgpu.mailer._send", side_effect=fake_send), \
         patch("iitgpu.mailer._admin_bcc", return_value=["alice@iit.lk", "admin@iit.lk"]):
        mailer.send_login_notification("alice", "alice@iit.lk", "127.0.0.1")
        import time; time.sleep(0.05)
    bcc = captured.get("bcc") or []
    assert "alice@iit.lk" not in bcc, "recipient must not appear in their own BCC"
    assert "admin@iit.lk" in bcc
