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
    assert "pytorch-2.7" in FRAMEWORK_PACKAGES
    assert "pytorch-2.7" in FRAMEWORK_LABELS
    pkg = " ".join(FRAMEWORK_PACKAGES["pytorch-2.7"])
    assert "cu128" in pkg
    assert "2.7" in pkg


def test_pytorch_26_is_first_in_labels():
    from iitgpu.envbuilder import FRAMEWORK_LABELS
    assert list(FRAMEWORK_LABELS.keys())[0] == "pytorch-2.7"


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
# Additional tests for Phase 1 — appended to test_envbuilder.py

def test_cu128_index_in_pytorch27_packages():
    """pytorch-2.7 must use cu128 index (the only one with sm_120 wheels)."""
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    pkg_str = " ".join(FRAMEWORK_PACKAGES["pytorch-2.7"])
    assert "https://download.pytorch.org/whl/cu128" in pkg_str, (
        "pytorch-2.7 must use cu128 index — cu126/cu124 lack sm_120 kernels"
    )


def test_pytorch27_pins_version_27_or_higher():
    """pytorch-2.7 entry must pin torch>=2.7 (first sm_120-capable release)."""
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    pkg_str = " ".join(FRAMEWORK_PACKAGES["pytorch-2.7"])
    # Accept torch==2.7.* or torch>=2.7
    assert "torch==2.7" in pkg_str or "torch>=2.7" in pkg_str, (
        "pytorch-2.7 must pin torch to >=2.7"
    )


def test_no_old_cuda_index_in_pytorch27():
    """pytorch-2.7 must NOT reference cu121, cu124, or cu126."""
    from iitgpu.envbuilder import FRAMEWORK_PACKAGES
    pkg_str = " ".join(FRAMEWORK_PACKAGES["pytorch-2.7"])
    for bad in ("cu121", "cu124", "cu126", "cu131"):
        assert bad not in pkg_str, (
            f"pytorch-2.7 references obsolete CUDA index {bad}; use cu128"
        )


def test_smoke_check_skips_gracefully_when_no_cuda(tmp_path):
    """_smoke_check_pytorch returns True and warns when CUDA is not available."""
    import subprocess
    from unittest.mock import patch, MagicMock
    from iitgpu.envbuilder import _smoke_check_pytorch

    no_gpu_output = "  torch 2.7.0\n  cuda available: False\n  [SKIP] no GPU on this node -- GPU smoke check skipped\n"
    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = no_gpu_output
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _smoke_check_pytorch("/fake/env/bin/python3", {})
    assert result is True


def test_smoke_check_returns_false_on_wrong_capability(tmp_path):
    """_smoke_check_pytorch returns False when the device is below sm_120."""
    import subprocess
    from unittest.mock import patch, MagicMock
    from iitgpu.envbuilder import _smoke_check_pytorch

    mock_result = MagicMock()
    mock_result.returncode = 2
    mock_result.stdout = "  torch 2.5.0\n  cuda available: True\n  device: RTX 3090  capability: sm_86\n"
    mock_result.stderr = "  FATAL: device capability sm_86 < sm_120."

    with patch("subprocess.run", return_value=mock_result):
        result = _smoke_check_pytorch("/fake/env/bin/python3", {})
    assert result is False


def test_smoke_check_returns_true_on_sm120(tmp_path):
    """_smoke_check_pytorch returns True when capability is (12, 0)."""
    import subprocess
    from unittest.mock import patch, MagicMock
    from iitgpu.envbuilder import _smoke_check_pytorch

    mock_result = MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = (
        "  torch 2.7.0\n"
        "  cuda available: True\n"
        "  device: NVIDIA GeForce RTX 5090  capability: sm_120\n"
        "  RTX 5090 / sm_120 confirmed -- running torch.compile matmul...\n"
        "  torch.compile matmul OK\n"
    )
    mock_result.stderr = ""

    with patch("subprocess.run", return_value=mock_result):
        result = _smoke_check_pytorch("/fake/env/bin/python3", {})
    assert result is True


def test_smoke_check_timeout_is_nonfatal():
    """A timeout in the smoke check is treated as a skip, not a failure."""
    import subprocess
    from unittest.mock import patch
    from iitgpu.envbuilder import _smoke_check_pytorch

    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="python3", timeout=120)):
        result = _smoke_check_pytorch("/fake/env/bin/python3", {})
    assert result is True


def test_build_env_pytorch_triggers_smoke_check(tmp_path, monkeypatch):
    """build_env calls _smoke_check_pytorch when framework is pytorch-2.7."""
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    smoke_called = []

    def fake_progress(cmd, phases, label, env=None):
        return 0, []

    def fake_pip_progress(cmd, label, env=None):
        return 0, []

    def fake_smoke(python_bin, pip_env):
        smoke_called.append(python_bin)
        return True  # pass

    with patch("shutil.which", return_value="/usr/bin/conda"), \
         patch("iitgpu.envbuilder._find_conda", return_value="/usr/bin/conda"), \
         patch("iitgpu.envbuilder._run_with_progress", side_effect=fake_progress), \
         patch("iitgpu.envbuilder._run_pip_with_progress", side_effect=fake_pip_progress), \
         patch("iitgpu.envbuilder._smoke_check_pytorch", side_effect=fake_smoke):
        from iitgpu.envbuilder import build_env
        from iitgpu.config import load_config
        success, path = build_env("testenv", "pytorch-2.7", None, load_config())

    assert success is True
    assert len(smoke_called) == 1, "smoke check should have been called once"


def test_build_env_bare_skips_smoke_check(tmp_path, monkeypatch):
    """build_env does NOT call _smoke_check_pytorch for the 'bare' framework."""
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    smoke_called = []

    def fake_progress(cmd, phases, label, env=None):
        return 0, []

    def fake_smoke(python_bin, pip_env):
        smoke_called.append(python_bin)
        return True

    with patch("shutil.which", return_value="/usr/bin/conda"), \
         patch("iitgpu.envbuilder._find_conda", return_value="/usr/bin/conda"), \
         patch("iitgpu.envbuilder._run_with_progress", side_effect=fake_progress), \
         patch("iitgpu.envbuilder._smoke_check_pytorch", side_effect=fake_smoke):
        from iitgpu.envbuilder import build_env
        from iitgpu.config import load_config
        success, path = build_env("testenv", "bare", None, load_config())

    assert success is True
    assert smoke_called == [], "smoke check should NOT be called for bare env"
