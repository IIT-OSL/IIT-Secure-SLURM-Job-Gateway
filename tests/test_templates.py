# tests/test_templates.py
import os
import pytest


def test_builtin_presets_gpu_count_matches_cluster(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.templates import _BUILTIN_PRESETS
    for name, preset in _BUILTIN_PRESETS.items():
        assert preset["gpus"] <= 1, f"Preset '{name}' requests {preset['gpus']} GPUs but cluster has 1"


def test_builtin_presets_cpus_within_cluster_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.templates import _BUILTIN_PRESETS
    for name, preset in _BUILTIN_PRESETS.items():
        assert preset["cpus"] <= 16, f"Preset '{name}' requests {preset['cpus']} CPUs but cluster has 16"


def test_builtin_presets_mem_within_cluster_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.templates import _BUILTIN_PRESETS
    for name, preset in _BUILTIN_PRESETS.items():
        assert preset["mem_gb"] <= 60, f"Preset '{name}' requests {preset['mem_gb']}GB but cluster has ~60GB"


def test_builtin_preset_partition_is_gpu(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    from iitgpu.templates import _BUILTIN_PRESETS
    for name, preset in _BUILTIN_PRESETS.items():
        assert preset["partition"] == "gpu", f"Preset '{name}' uses partition '{preset['partition']}' not 'gpu'"
