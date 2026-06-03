# tests/test_mailer.py
"""Phases 1-3 — credential safety and reliable delivery tests for iitgpu/mailer.py."""
from unittest.mock import patch, MagicMock
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


# ── Phase 2: no hardcoded key, urllib transport ───────────────────────────────

def test_send_skips_when_no_api_key(capsys):
    """_send must not attempt a network call when RESEND_API_KEY is unset."""
    with patch("iitgpu.mailer._resend_key", return_value=""), \
         patch("iitgpu.mailer.urllib.request.urlopen") as mock_urlopen:
        mailer._send("a@b.com", "subj", "<html/>")
    mock_urlopen.assert_not_called()


def test_send_uses_urllib_not_subprocess():
    """Transport must use urllib, never subprocess (key must not appear in argv)."""
    import subprocess as _subprocess
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    with patch("iitgpu.mailer._resend_key", return_value="re_test_key"), \
         patch("iitgpu.mailer.urllib.request.urlopen", return_value=mock_resp), \
         patch("iitgpu.mailer.urllib.request.Request") as mock_req_cls, \
         patch.object(_subprocess, "run") as mock_sub:
        mock_req_cls.return_value = MagicMock()
        mailer._send("a@b.com", "subj", "<html/>")
    mock_sub.assert_not_called()
    # Key must be in header, not in any arg list
    req_call = mock_req_cls.call_args
    headers = req_call[1]["headers"] if req_call[1] else req_call[0][2]
    assert "re_test_key" in headers.get("Authorization", "")


def test_api_key_not_in_module_constants():
    """No hardcoded production key should exist as a module-level constant."""
    import inspect
    src = inspect.getsource(mailer)
    # A real Resend key starts with "re_" followed by alphanumerics
    import re as _re
    matches = _re.findall(r're_[A-Za-z0-9]{10,}', src)
    assert not matches, f"Hardcoded Resend key(s) found in mailer.py: {matches}"


# ── Phase 3: synchronous delivery for must-deliver emails ─────────────────────

def test_send_welcome_returns_success_result():
    """send_welcome must return (True, 'sent') on success."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    with patch("iitgpu.mailer._resend_key", return_value="re_test"), \
         patch("iitgpu.mailer.urllib.request.urlopen", return_value=mock_resp), \
         patch("iitgpu.mailer.urllib.request.Request", return_value=MagicMock()):
        ok, msg = mailer.send_welcome("alice", "alice@iit.lk")
    assert ok is True
    assert msg == "sent"


def test_send_welcome_returns_failure_on_http_error():
    """send_welcome must return (False, error_msg) on HTTP failure."""
    import urllib.error as _ue
    exc = _ue.HTTPError("url", 422, "Unprocessable", {}, None)
    with patch("iitgpu.mailer._resend_key", return_value="re_test"), \
         patch("iitgpu.mailer.urllib.request.urlopen", side_effect=exc), \
         patch("iitgpu.mailer.urllib.request.Request", return_value=MagicMock()):
        ok, msg = mailer.send_welcome("alice", "alice@iit.lk")
    assert ok is False
    assert "422" in msg


def test_send_offboard_returns_tuple():
    """send_offboard must return a (bool, str) tuple."""
    mock_resp = MagicMock()
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.status = 200
    with patch("iitgpu.mailer._resend_key", return_value="re_test"), \
         patch("iitgpu.mailer.urllib.request.urlopen", return_value=mock_resp), \
         patch("iitgpu.mailer.urllib.request.Request", return_value=MagicMock()), \
         patch("iitgpu.mailer._admin_bcc", return_value=[]):
        result = mailer.send_offboard("alice", "alice@iit.lk")
    assert isinstance(result, tuple) and len(result) == 2
    assert isinstance(result[0], bool)
