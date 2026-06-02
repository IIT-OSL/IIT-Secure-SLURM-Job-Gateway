# tests/test_models.py
"""Model download must not OOM-kill the RAM-constrained login node.

Regression: huggingface_hub auto-used the Xet backend (hf_xet), which buffered
large chunks in memory and got OOM-killed mid-download on multi-GB models,
tearing down the SSH/TUI session ("Connection to login-node closed").
"""
import os
from pathlib import Path
from unittest.mock import patch

import huggingface_hub.constants as hf_constants


def _cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "models").mkdir(parents=True, exist_ok=True)
    from iitgpu.config import load_config
    return load_config()


def test_download_hf_disables_xet_and_limits_workers(tmp_path, monkeypatch):
    from iitgpu import models
    cfg = _cfg(tmp_path, monkeypatch)

    monkeypatch.delenv("HF_HUB_DISABLE_XET", raising=False)
    saved_const = hf_constants.HF_HUB_DISABLE_XET
    captured = {}

    def fake_snapshot(**kwargs):
        captured.update(kwargs)
        captured["_xet_env"] = os.environ.get("HF_HUB_DISABLE_XET")
        captured["_xet_const"] = hf_constants.HF_HUB_DISABLE_XET
        Path(kwargs["local_dir"]).mkdir(parents=True, exist_ok=True)
        return kwargs["local_dir"]

    try:
        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot), \
             patch("iitgpu.models.register_model"):
            ok, res = models.download_hf(cfg, "mistralai/Mistral-7B-v0.1")
    finally:
        hf_constants.HF_HUB_DISABLE_XET = saved_const

    assert ok is True, res
    # Xet backend disabled both ways at call time
    assert captured["_xet_env"] == "1"
    assert captured["_xet_const"] is True
    # bounded concurrency
    assert captured.get("max_workers", 99) <= 4
    # deprecated symlink param removed (it triggered a UserWarning)
    assert "local_dir_use_symlinks" not in captured
    # dest is name-sanitised under the models dir
    assert captured["local_dir"].endswith("mistralai--Mistral-7B-v0.1")


def test_download_hf_audit_includes_path_and_size(tmp_path, monkeypatch):
    from iitgpu import models
    cfg = _cfg(tmp_path, monkeypatch)
    logged = []

    def fake_snapshot(**kwargs):
        Path(kwargs["local_dir"]).mkdir(parents=True, exist_ok=True)
        return kwargs["local_dir"]

    import huggingface_hub.constants as hf_constants
    saved = hf_constants.HF_HUB_DISABLE_XET
    try:
        with patch("huggingface_hub.snapshot_download", side_effect=fake_snapshot), \
             patch("iitgpu.models.register_model"), \
             patch("iitgpu.models.auditclient.log",
                   side_effect=lambda *a, **kw: logged.append((a, kw))):
            models.download_hf(cfg, "org/repo")
    finally:
        hf_constants.HF_HUB_DISABLE_XET = saved

    ok_events = [(a, kw) for a, kw in logged if a and a[0] == "model_download_ok"]
    assert ok_events, "model_download_ok not emitted"
    meta = ok_events[0][1].get("meta", {})
    assert "path" in meta, f"meta missing 'path': {meta}"
    assert "size_mb" in meta, f"meta missing 'size_mb': {meta}"


def test_download_url_audit_includes_path_and_size(tmp_path, monkeypatch):
    from iitgpu import models
    cfg = _cfg(tmp_path, monkeypatch)
    logged = []

    def fake_retrieve(url, dest):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(b"x" * 1024)

    with patch("urllib.request.urlretrieve", side_effect=fake_retrieve), \
         patch("iitgpu.models.register_model"), \
         patch("iitgpu.models.auditclient.log",
               side_effect=lambda *a, **kw: logged.append((a, kw))):
        models.download_url(cfg, "https://example.com/model.bin", "mymodel")

    ok_events = [(a, kw) for a, kw in logged if a and a[0] == "model_download_ok"]
    assert ok_events, "model_download_ok not emitted"
    meta = ok_events[0][1].get("meta", {})
    assert "path" in meta, f"meta missing 'path': {meta}"
    assert "size_mb" in meta, f"meta missing 'size_mb': {meta}"
