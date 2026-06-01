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
         patch("iitgpu.envbuilder._run_pip_with_progress", return_value=(0, [])), \
         patch("iitgpu.setup._verify_spec_packages", return_value=[]), \
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


def test_parse_spec_pip_deps_extracts_pip_block():
    """The spec parser returns every pip: item, splitting flag+value tokens."""
    from iitgpu import setup as s
    spec = s._PREBUILT_SPECS_DIR / "llm-finetune.yml"
    assert spec.exists(), "llm-finetune.yml spec must exist"
    deps = s._parse_spec_pip_deps(str(spec))
    assert "torch==2.7.*" in deps
    assert "transformers>=4.40" in deps
    # "--extra-index-url URL" must be split into two separate pip args
    assert "--extra-index-url" in deps
    assert "https://download.pytorch.org/whl/cu128" in deps
    # conda-only deps (python, the bare `pip` package) must NOT leak in
    assert not any(d.startswith("python=") for d in deps)


def test_install_prebuilt_refuses_to_register_incomplete_env(tmp_path, monkeypatch):
    """If conda env create exits 0 but the pip stage left packages missing
    (the llm-finetune 'No module named torch' failure), the installer must
    detect it, refuse to register the env, and not report success.
    """
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "envs").mkdir()

    from iitgpu import setup as s
    from iitgpu.config import load_config
    cfg = load_config()

    name = next(
        n for n in s._PREBUILT_DESCRIPTIONS
        if (s._PREBUILT_SPECS_DIR / f"{n}.yml").exists()
    )
    sel = MagicMock()
    sel.ask.return_value = f"{name}  — {s._PREBUILT_DESCRIPTIONS[name]}"

    saved = {"called": False}

    def fake_save(*a, **k):
        saved["called"] = True

    with patch("iitgpu.setup.questionary.select", return_value=sel), \
         patch("iitgpu.envbuilder._find_conda", return_value="/shared/miniforge3/bin/conda"), \
         patch("iitgpu.envbuilder._run_with_progress", return_value=(0, [])), \
         patch("iitgpu.envbuilder._run_pip_with_progress", return_value=(0, [])), \
         patch("iitgpu.setup._verify_spec_packages", return_value=["torch", "transformers"]), \
         patch("iitgpu.setup.auditclient"), \
         patch("iitgpu.envs._save_venv_registry", side_effect=fake_save), \
         patch("iitgpu.envs._load_venv_registry", return_value=[]):
        s._run_install_prebuilt(cfg)

    assert saved["called"] is False, (
        "installer registered an env that failed package verification"
    )


def test_run_setup_uses_arrow_select_menu_not_confirm_chain(monkeypatch):
    """Setup shows one arrow-key select menu (all actions at once), dispatches
    the chosen action, loops, and exits on 'Back to main menu' — it must NOT
    ask a yes/no confirm per step.
    """
    from iitgpu import setup as s

    calls = []
    select_returns = iter(["Model download", "Smoke test", "Back to main menu"])
    sel = MagicMock()
    sel.ask.side_effect = lambda: next(select_returns)
    confirm_mock = MagicMock()

    with patch("iitgpu.setup.load_config", return_value=MagicMock()), \
         patch("iitgpu.setup._run_health_check", return_value=True), \
         patch("iitgpu.setup.questionary.select", return_value=sel) as select_patch, \
         patch("iitgpu.setup.questionary.confirm", confirm_mock), \
         patch("iitgpu.setup._run_model_download", side_effect=lambda cfg: calls.append("model")), \
         patch("iitgpu.setup._run_smoke_test", side_effect=lambda cfg: calls.append("smoke")):
        s.run_setup()

    # both chosen actions ran, in order, then the menu exited
    assert calls == ["model", "smoke"]
    # menu was shown once per loop iteration (2 actions + the exit choice)
    assert select_patch.call_count >= 1 and sel.ask.call_count == 3
    # no per-step yes/no confirm was used as navigation
    confirm_mock.assert_not_called()

    # every action must appear in the one menu's choice list
    choices = select_patch.call_args.kwargs["choices"]
    for label in ("Environment (conda/venv)", "Install a prebuilt environment",
                  "Manage environments & containers", "Data upload",
                  "Model download", "Smoke test"):
        assert label in choices
    assert "Back to main menu" in choices


def test_run_setup_back_exits_without_running_steps(monkeypatch):
    from iitgpu import setup as s
    sel = MagicMock()
    sel.ask.return_value = "Back to main menu"
    ran = []
    with patch("iitgpu.setup.load_config", return_value=MagicMock()), \
         patch("iitgpu.setup._run_health_check", return_value=True), \
         patch("iitgpu.setup.questionary.select", return_value=sel), \
         patch("iitgpu.setup._run_model_download", side_effect=lambda cfg: ran.append("x")):
        s.run_setup()
    assert ran == []
