# tests/test_permissions.py
"""Shared-state files must be writable by every gateway user.

The cluster's NFS export uses root_squash and supports neither ACLs nor setgid
group inheritance, so group-based sharing can't be set up by the installer.
Whoever creates a shared registry/template makes it world-writable (0666) so any
other gpuusers member can update it in place. Regression for:
"Permission denied: '/shared/models/.registry.json'".
"""
import os
import stat
from unittest.mock import patch

from iitgpu.config import load_config, make_shared_writable


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_make_shared_writable_file_becomes_0666(tmp_path):
    f = tmp_path / "x.json"
    f.write_text("{}")
    os.chmod(f, 0o600)
    make_shared_writable(f)
    assert _mode(f) == 0o666


def test_make_shared_writable_dir_becomes_0777(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    os.chmod(d, 0o700)
    make_shared_writable(d)
    assert _mode(d) == 0o777


def test_make_shared_writable_missing_path_is_silent(tmp_path):
    make_shared_writable(tmp_path / "does-not-exist")  # must not raise


def test_model_registry_is_world_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "models").mkdir()
    from iitgpu.models import ModelEntry, register_model, _registry_path
    cfg = load_config()
    with patch("iitgpu.models.auditclient"):
        register_model(cfg, ModelEntry(
            name="m", source="huggingface", path="/x",
            added_at="t", added_by="u", size_mb=1,
        ))
    assert _mode(_registry_path(cfg)) == 0o666


def test_env_registry_is_world_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    (tmp_path / "models").mkdir()
    from iitgpu.envs import EnvEntry, _save_venv_registry, _envs_registry_path
    cfg = load_config()
    _save_venv_registry(cfg, [EnvEntry(name="e", kind="conda", path="/x")])
    assert _mode(_envs_registry_path(cfg)) == 0o666


def test_install_sh_sets_group_writable_umask_in_launcher():
    from pathlib import Path
    sh = (Path(__file__).parent.parent / "deploy" / "install.sh").read_text()
    # launcher must set a group-writable umask so TUI-created files are sharable
    assert "umask 002" in sh
    # installer must group-own shared dirs to gpuusers and try setgid + ACLs
    assert "gpuusers" in sh and "g+s" in sh


def test_install_sh_grants_audit_daemon_read_access():
    """The audit daemon (gpusync) must be able to read deploy/audit_daemon.py from
    the 0750 install tree, or it crash-loops and audit logging is refused."""
    from pathlib import Path
    sh = (Path(__file__).parent.parent / 'deploy' / 'install.sh').read_text()
    assert 'usermod -aG gpuusers gpusync' in sh
