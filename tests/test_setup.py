# tests/test_setup.py
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest


def test_health_check_passes_when_shared_writable_and_sinfo_ok(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "envs").mkdir()

    with patch("iitgpu.slurm.get_partitions", return_value=[MagicMock()]):
        from iitgpu.setup import check_cluster_health
        from iitgpu.config import load_config
        ok, messages = check_cluster_health(load_config())

    assert ok is True
    assert len(messages) == 0


def test_health_check_fails_when_sinfo_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    with patch("iitgpu.slurm.get_partitions", return_value=[]):
        from iitgpu.setup import check_cluster_health
        from iitgpu.config import load_config
        ok, messages = check_cluster_health(load_config())

    assert ok is False
    assert any("sinfo" in m.lower() or "cluster" in m.lower() for m in messages)


def test_health_check_fails_when_shared_not_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))

    with patch("iitgpu.slurm.get_partitions", return_value=[MagicMock()]), \
         patch("os.access", return_value=False):
        from iitgpu.setup import check_cluster_health
        from iitgpu.config import load_config
        import importlib
        import iitgpu.setup as s
        importlib.reload(s)
        ok, messages = s.check_cluster_health(load_config())

    assert ok is False


def test_smoke_test_script_contains_cuda_check(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.setup import _build_smoke_test_script
    from iitgpu.config import load_config
    script = _build_smoke_test_script("/shared/envs/pytorch-2.5", load_config())
    assert "torch.cuda.is_available()" in script
    assert "#SBATCH --gres=gpu:1" in script


def test_smoke_test_script_output_under_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.setup import _build_smoke_test_script
    from iitgpu.config import load_config
    script = _build_smoke_test_script("/shared/envs/pytorch-2.5", load_config())
    assert str(tmp_path) in script or "/shared" in script
