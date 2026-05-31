# tests/test_adduser_wrapper.py
"""Tests for the interactive addUser.sh wrapper."""
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
WRAPPER = REPO / "addUser.sh"


def test_wrapper_exists_and_executable():
    assert WRAPPER.exists(), "addUser.sh missing at repo root"
    assert WRAPPER.stat().st_mode & 0o111, "addUser.sh not executable"


def test_wrapper_passes_bash_syntax():
    r = subprocess.run(["bash", "-n", str(WRAPPER)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_wrapper_delegates_to_iit_gpu_adduser():
    text = WRAPPER.read_text()
    assert "iit-gpu-adduser" in text, "wrapper must call the real provisioning script"


def test_wrapper_rejects_invalid_username_then_cancels_on_eof():
    # Feed one invalid username; EOF should cancel without spinning forever.
    r = subprocess.run(["bash", str(WRAPPER)], input="BAD NAME\n",
                       capture_output=True, text=True, timeout=10,
                       env={"PATH": "/usr/bin:/bin", "IIT_SITE_ENV": "/dev/null"})
    out = r.stdout + r.stderr
    assert "Invalid" in out
    assert "cancelled" in out.lower()
    assert r.returncode != 0
