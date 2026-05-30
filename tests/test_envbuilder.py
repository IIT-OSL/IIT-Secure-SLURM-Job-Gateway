# tests/test_envbuilder.py
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def test_framework_packages_contains_pytorch():
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    assert "pytorch-2.5" in FRAMEWORK_PACKAGES


def test_framework_packages_contains_pytorch_26():
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES, FRAMEWORK_LABELS
    assert "pytorch-2.6" in FRAMEWORK_PACKAGES
    assert "pytorch-2.6" in FRAMEWORK_LABELS
    pkg = " ".join(FRAMEWORK_PACKAGES["pytorch-2.6"])
    assert "cu128" in pkg
    assert "2.6" in pkg


def test_pytorch_26_is_first_in_labels():
    from iitgpu.envbuilder import FRAMEWORK_LABELS
    assert list(FRAMEWORK_LABELS.keys())[0] == "pytorch-2.6"


def test_framework_packages_contains_tensorflow():
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    assert "tensorflow-2.18" in FRAMEWORK_PACKAGES


def test_framework_packages_contains_jax():
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    assert "jax-0.4" in FRAMEWORK_PACKAGES


def test_framework_packages_contains_bare():
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    assert "bare" in FRAMEWORK_PACKAGES
    assert FRAMEWORK_PACKAGES["bare"] == []


def test_build_env_returns_false_when_conda_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    with patch("shutil.which", return_value=None), \
         patch("iitgpu.envbuilder._find_conda", return_value=None):
        from iitgpu.envbuilder import build_env
        from iitgpu.config import load_config
        success, path = build_env("testenv", "pytorch-2.5", None, load_config())
    assert success is False
    assert path == ""


def test_build_env_success_calls_conda_create(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    calls: list[list[str]] = []

    def fake_progress(cmd, phases, label, env=None):
        calls.append(cmd)
        return 0, []

    with patch("shutil.which", return_value="/usr/bin/conda"), \
         patch("iitgpu.envbuilder._run_with_progress", side_effect=fake_progress):
        from iitgpu.envbuilder import build_env
        from iitgpu.config import load_config
        success, path = build_env("testenv", "bare", None, load_config())

    assert success is True
    assert any("conda" in str(c[0]) for c in calls)


def test_build_env_unknown_framework_returns_false(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    with patch("shutil.which", return_value="/usr/bin/conda"):
        from iitgpu.envbuilder import build_env
        from iitgpu.config import load_config
        success, path = build_env("testenv", "unknown_framework_xyz", None, load_config())
    assert success is False
