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
    out_dir = str(tmp_path / "jobs" / "testuser" / "smoke_test")
    script = _build_smoke_test_script("/shared/envs/pytorch-2.5", load_config(), out_dir)
    assert "torch.cuda.is_available()" in script
    assert "#SBATCH --gres=gpu:1" in script


def test_smoke_test_script_output_under_shared(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.setup import _build_smoke_test_script
    from iitgpu.config import load_config
    out_dir = str(tmp_path / "jobs" / "testuser" / "smoke_test")
    script = _build_smoke_test_script("/shared/envs/pytorch-2.5", load_config(), out_dir)
    assert "slurm-%j.out" in script
    assert out_dir in script


def test_install_prebuilt_uses_yes_not_removed_force(tmp_path, monkeypatch):
    """conda >=24 removed `conda env create --force`; ensure we pass --yes instead.

    Regression for the cluster failure: `conda: error: unrecognized
    arguments: --force` (conda 26.x) when installing a prebuilt env.
    """
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "envs").mkdir()

    from iitgpu import setup as s
    from iitgpu.config import load_config
    cfg = load_config()

    available = [
        n for n in s._PREBUILT_DESCRIPTIONS
        if (s._PREBUILT_SPECS_DIR / f"{n}.yml").exists()
    ]
    assert available, "expected prebuilt specs to exist in envs/specs/"
    name = available[0]
    choice = f"{name}  — {s._PREBUILT_DESCRIPTIONS[name]}"

    captured = {}

    def fake_run_with_progress(argv, phases, label, env=None):
        captured["argv"] = argv
        captured["env"] = env
        return 0, []

    sel = MagicMock()
    sel.ask.return_value = choice

    with patch("iitgpu.setup.questionary.select", return_value=sel), \
         patch("iitgpu.envbuilder._find_conda", return_value="/shared/miniforge3/bin/conda"), \
         patch("iitgpu.envbuilder._run_with_progress", side_effect=fake_run_with_progress), \
         patch("iitgpu.setup.auditclient"), \
         patch("iitgpu.envs._save_venv_registry"), \
         patch("iitgpu.envs._load_venv_registry", return_value=[]):
        s._run_install_prebuilt(cfg)

    argv = captured.get("argv")
    assert argv is not None, "conda env create was never invoked"
    assert argv[1:3] == ["env", "create"], f"unexpected conda argv: {argv}"
    assert "--force" not in argv, "must not use removed `conda env create --force` flag"
    assert "--yes" in argv or "-y" in argv, f"expected --yes to auto-confirm: {argv}"

    # TMPDIR must point at roomy shared storage (under nfs_root), not the
    # default /tmp tmpfs, so multi-GB CUDA wheels don't overflow during unpack.
    env = captured.get("env")
    assert env is not None, "installer must pass an env with TMPDIR set"
    assert env.get("TMPDIR", "").startswith(str(tmp_path)), (
        f"TMPDIR should be under nfs_root ({tmp_path}), got {env.get('TMPDIR')!r}"
    )
