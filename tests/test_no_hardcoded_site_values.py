# tests/test_no_hardcoded_site_values.py
"""Open-source guard: site-specific constants must live in config/site.env, not code."""
import re
from pathlib import Path

IITGPU = Path(__file__).parent.parent / "iitgpu"

# Patterns that indicate a leaked site constant (IPs, the gateway port, the GID).
_FORBIDDEN = [
    re.compile(r"192\.168\."),
    re.compile(r"10\.35\."),
    re.compile(r"\b2225\b"),
    re.compile(r'"sudo", "-u", "daham"'),
]


def _py_files():
    return [p for p in IITGPU.rglob("*.py") if "__pycache__" not in str(p)]


def test_no_hardcoded_site_values_in_iitgpu():
    offenders = []
    for f in _py_files():
        text = f.read_text()
        for pat in _FORBIDDEN:
            for m in pat.finditer(text):
                line = text[:m.start()].count("\n") + 1
                offenders.append(f"{f.name}:{line}  matches {pat.pattern!r}")
    assert not offenders, "Hardcoded site values found (move to config/site.env):\n" + "\n".join(offenders)
