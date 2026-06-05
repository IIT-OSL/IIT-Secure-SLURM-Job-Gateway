# tests/test_mailer.py
"""Mail safety tests — mailer.py builds messages and hands them to the daemon.

The Resend API key lives ONLY on the daemon; mailer.py must contain no key,
no urllib, and must route every send through _daemon_mail.
"""
from unittest.mock import patch
import pytest
from iitgpu import mailer


def _capture():
    """Patch _daemon_mail and capture its call args. Returns (ctx, store)."""
    store = {}
    def fake(to, subject, html, bcc=None, kind="generic", ip=""):
        store.update(to=to, subject=subject, html=html, bcc=bcc, kind=kind, ip=ip)
        return True, "sent"
    return patch("iitgpu.mailer._daemon_mail", side_effect=fake), store


class _SyncThread:
    """Drop-in for threading.Thread that runs target synchronously on start()."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}
    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)
    def join(self, timeout=None):
        pass



# ── C1: no secret material anywhere in mailer.py ──────────────────────────────

def test_mailer_has_no_api_key_or_urllib():
    import inspect, re
    src = inspect.getsource(mailer)
    assert not re.findall(r're_[A-Za-z0-9]{10,}', src), "hardcoded key in mailer.py"
    assert "urllib" not in src, "mailer.py must not do its own HTTP (key stays on daemon)"
    assert "_resend_key" not in src, "mailer.py must not read the API key"


def test_all_sends_route_through_daemon():
    ctx, store = _capture()
    with ctx:
        mailer.send_welcome("alice", "alice@iit.lk")
    assert store, "send_welcome must call _daemon_mail"


# ── welcome ───────────────────────────────────────────────────────────────────

def test_welcome_recipient_and_kind():
    ctx, store = _capture()
    with ctx:
        mailer.send_welcome("alice", "alice@iit.lk", "Alice")
    assert store["to"] == "alice@iit.lk"
    assert store["kind"] == "welcome"


def test_welcome_includes_password_when_given():
    ctx, store = _capture()
    with ctx:
        mailer.send_welcome("alice", "alice@iit.lk", "Alice", password="s3cr3t!")
    assert "Initial Password" in store["html"]
    assert "s3cr3t!" in store["html"]


def test_welcome_omits_password_row_when_blank():
    ctx, store = _capture()
    with ctx:
        mailer.send_welcome("alice", "alice@iit.lk", "Alice")
    assert "Initial Password" not in store["html"]


def test_welcome_html_escapes_password():
    ctx, store = _capture()
    with ctx:
        mailer.send_welcome("alice", "alice@iit.lk", "Alice", password="a<b>&\"c")
    assert "a<b>&\"c" not in store["html"]
    assert "a&lt;b&gt;&amp;&quot;c" in store["html"]


def test_welcome_no_bcc():
    ctx, store = _capture()
    with ctx:
        mailer.send_welcome("alice", "alice@iit.lk")
    assert not store["bcc"]


def test_welcome_signature_has_password_param():
    import inspect
    assert "password" in inspect.signature(mailer.send_welcome).parameters


def test_welcome_returns_tuple():
    with patch("iitgpu.mailer._daemon_mail", return_value=(True, "sent")):
        result = mailer.send_welcome("alice", "alice@iit.lk")
    assert result == (True, "sent")


def test_welcome_propagates_failure():
    with patch("iitgpu.mailer._daemon_mail", return_value=(False, "HTTP 422")):
        ok, msg = mailer.send_welcome("alice", "alice@iit.lk")
    assert ok is False and "422" in msg


# ── offboard ──────────────────────────────────────────────────────────────────

def test_offboard_kind_and_tuple():
    ctx, store = _capture()
    with ctx:
        result = mailer.send_offboard("alice", "alice@iit.lk", "Alice")
    assert store["kind"] == "offboard"
    assert isinstance(result, tuple) and isinstance(result[0], bool)


def test_offboard_no_bcc():
    """Offboard mail goes only to the user — admins are never BCC'd on it."""
    ctx, store = _capture()
    with ctx:
        mailer.send_offboard("alice", "alice@iit.lk")
    assert not store["bcc"]


# ── login notification ────────────────────────────────────────────────────────

def test_login_kind_and_ip():
    ctx, store = _capture()
    with ctx, patch("iitgpu.mailer.Thread", _SyncThread):
        mailer.send_login_notification("alice", "alice@iit.lk", "10.35.4.9")
    assert store["kind"] == "login"
    assert store["ip"] == "10.35.4.9"


def test_login_no_client_bcc():
    """BCC is computed by the daemon, not the client."""
    ctx, store = _capture()
    with ctx, patch("iitgpu.mailer.Thread", _SyncThread):
        mailer.send_login_notification("alice", "alice@iit.lk", "10.0.0.1")
    assert not store["bcc"]


def test_login_local_fallback_ip():
    ctx, store = _capture()
    with ctx, patch("iitgpu.mailer.Thread", _SyncThread):
        mailer.send_login_notification("alice", "alice@iit.lk", "")
    assert store["ip"] == "local"


# ── Regression: standalone SLURM MailProg (deploy/iit-gpu-mailer) fallback ────
# slurmctld runs this as the `slurm` user. Its msmtp fallback once passed `-s`,
# which msmtp rejects ("invalid option -- 's'"), so when Resend was unreachable
# no mail went out at all. The fallback must pipe a full RFC-822 message to
# msmtp with the recipient as a positional arg and NO `-s` flag.

def _load_job_mailer():
    import importlib.util, pathlib
    from importlib.machinery import SourceFileLoader
    p = pathlib.Path(__file__).resolve().parent.parent / "deploy" / "iit-gpu-mailer"
    loader = SourceFileLoader("iit_gpu_jobmailer", str(p))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_job_mailer_fallback_does_not_pass_dash_s():
    jm = _load_job_mailer()
    captured = {}

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["input"] = kw.get("input", "")
        class R: returncode = 0
        return R()

    with patch.object(jm.subprocess, "run", side_effect=fake_run):
        jm.fallback_msmtp("user@example.com", "My Subject", "the body")

    argv = captured["argv"]
    assert "-s" not in argv, "msmtp has no -s flag; fallback must not pass it"
    assert argv[-1] == "user@example.com", "recipient must be the positional arg"
    # Subject must travel in the message headers piped on stdin instead.
    assert "Subject: My Subject" in captured["input"]
    assert "the body" in captured["input"]
