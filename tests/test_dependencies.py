# tests/test_dependencies.py
"""Guard: every third-party module imported by the tool must be declared in
requirements.txt, and install.sh must install from that file.

Regression for: model download failed with 'huggingface_hub not installed'
because install.sh hardcoded 'pip3 install rich questionary' and never
installed huggingface_hub (nor even prompt_toolkit from requirements.txt).
"""
import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent
PKG_DIR = REPO_ROOT / "iitgpu"
REQUIREMENTS = REPO_ROOT / "requirements.txt"
INSTALL_SH = REPO_ROOT / "deploy" / "install.sh"

# import-name -> distribution-name when they differ (none needed today, but
# kept explicit so future renames are obvious).
_NORMALISE = lambda s: s.lower().replace("_", "-")


def _stdlib_names():
    names = set(getattr(sys, "stdlib_module_names", set()))
    names |= {"__future__"}
    return names


def _imported_top_level_modules():
    """All top-level modules imported anywhere under iitgpu/ (incl. nested)."""
    mods = set()
    for py in PKG_DIR.rglob("*.py"):
        tree = ast.parse(py.read_text(), filename=str(py))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    mods.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:  # absolute imports only
                    mods.add(node.module.split(".")[0])
    return mods


def _declared_requirements():
    reqs = set()
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = re.split(r"[<>=!~ \[;]", line, maxsplit=1)[0].strip()
        if name:
            reqs.add(_NORMALISE(name))
    return reqs


def test_all_third_party_imports_are_declared():
    stdlib = _stdlib_names()
    declared = _declared_requirements()
    third_party = {
        m for m in _imported_top_level_modules()
        if m not in stdlib and m != "iitgpu"
    }
    missing = {m for m in third_party if _NORMALISE(m) not in declared}
    assert not missing, (
        f"Third-party imports not declared in requirements.txt: {sorted(missing)}. "
        f"Add them so deploy/install.sh installs them."
    )


def test_huggingface_hub_is_declared():
    assert "huggingface-hub" in _declared_requirements(), (
        "huggingface_hub must be in requirements.txt — model download needs it"
    )


def test_install_sh_installs_from_requirements_file():
    text = INSTALL_SH.read_text()
    assert "-r " in text and "requirements.txt" in text, (
        "install.sh must 'pip3 install -r requirements.txt', not a hardcoded subset"
    )
    # the old hardcoded form must be gone
    assert not re.search(r"pip3 install[^\n]*break-system-packages rich questionary\s*$", text, re.M), (
        "install.sh still hardcodes 'rich questionary' instead of using requirements.txt"
    )
