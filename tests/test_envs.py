# tests/test_envs.py
"""Shared env registry: prebuilt conda envs must survive load/save and be
visible to all users (not just the one whose conda discovered them).

Regression: _load_venv_registry filtered to kind=='venv', so prebuilt conda
envs were dropped on load -> invisible cross-user, and each prebuilt install
overwrote the previous registry entry.
"""
from unittest.mock import patch

from iitgpu.config import load_config
from iitgpu.envs import (
    EnvEntry,
    _load_venv_registry,
    _save_venv_registry,
    list_all_envs,
)


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    return load_config()


def test_registry_roundtrips_conda_and_venv_entries(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    entries = [
        EnvEntry(name="llm-finetune", kind="conda", path="/shared/envs/llm-finetune"),
        EnvEntry(name="myvenv", kind="venv", path="/shared/envs/myvenv"),
    ]
    _save_venv_registry(cfg, entries)
    loaded = {e.name: e.kind for e in _load_venv_registry(cfg)}
    assert loaded == {"llm-finetune": "conda", "myvenv": "venv"}


def test_second_prebuilt_install_does_not_wipe_first(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    # mimic setup.py's _run_install_prebuilt: load -> drop same name -> append -> save
    def install(name):
        envs = [e for e in _load_venv_registry(cfg) if e.name != name]
        envs.append(EnvEntry(name=name, kind="conda", path=f"/shared/envs/{name}"))
        _save_venv_registry(cfg, envs)

    install("data-science")
    install("llm-finetune")
    names = {e.name for e in _load_venv_registry(cfg)}
    assert names == {"data-science", "llm-finetune"}, (
        "installing a second prebuilt env must not drop the first"
    )


def test_list_all_envs_surfaces_registry_conda_when_discovery_misses_it(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, monkeypatch)
    _save_venv_registry(cfg, [
        EnvEntry(name="llm-finetune", kind="conda", path="/shared/envs/llm-finetune"),
    ])
    # another user's conda discovers nothing (env not in their environments.txt)
    with patch("iitgpu.envs.discover_conda_envs", return_value=[]):
        names = {e.name for e in list_all_envs(cfg)}
    assert "llm-finetune" in names
