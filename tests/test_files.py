# tests/test_files.py
"""Phase 5: jailed file operations + env/container delete."""
from pathlib import Path
from unittest.mock import patch
import importlib
import pytest


@pytest.fixture
def jailed(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    import iitgpu.validate as v
    importlib.reload(v)
    return tmp_path


def test_list_dir_jailed(jailed):
    (jailed / "a.txt").write_text("x")
    (jailed / "sub").mkdir()
    from iitgpu.files import list_dir
    names = [e.name for e in list_dir(str(jailed))]
    assert "sub" in names and "a.txt" in names
    assert list_dir(str(jailed))[0].is_dir


def test_list_dir_outside_jail_empty(jailed):
    from iitgpu.files import list_dir
    assert list_dir("/etc") == []


def test_make_dir_in_jail(jailed):
    from iitgpu.files import make_dir
    ok, path = make_dir(str(jailed), "newdir")
    assert ok and Path(path).is_dir()


def test_make_dir_rejects_traversal(jailed):
    from iitgpu.files import make_dir
    ok, msg = make_dir(str(jailed), "..")
    assert ok is False


def test_make_dir_rejects_bad_name(jailed):
    from iitgpu.files import make_dir
    ok, _ = make_dir(str(jailed), "bad name!")
    assert ok is False


def test_delete_path_jailed(jailed):
    f = jailed / "del.txt"; f.write_text("x")
    from iitgpu.files import delete_path
    ok, _ = delete_path(str(f))
    assert ok and not f.exists()


def test_delete_path_outside_jail_refused(jailed):
    from iitgpu.files import delete_path
    ok, msg = delete_path("/etc/hostname")
    assert ok is False and "denied" in msg.lower()


def test_rename_path(jailed):
    f = jailed / "old.txt"; f.write_text("x")
    from iitgpu.files import rename_path
    ok, dest = rename_path(str(f), "new.txt")
    assert ok and Path(dest).name == "new.txt"


def test_rename_rejects_escape(jailed):
    f = jailed / "x.txt"; f.write_text("y")
    from iitgpu.files import rename_path
    ok, _ = rename_path(str(f), "../escaped.txt")
    assert ok is False


def test_copy_path(jailed):
    f = jailed / "src.txt"; f.write_text("data")
    d = jailed / "dst"; d.mkdir()
    from iitgpu.files import copy_path
    ok, dest = copy_path(str(f), str(d))
    assert ok and Path(dest).read_text() == "data"


def test_disk_usage_returns_triple(jailed):
    from iitgpu.files import disk_usage
    total, used, free = disk_usage(str(jailed))
    assert total > 0 and free >= 0


def test_disk_usage_outside_jail_zero(jailed):
    from iitgpu.files import disk_usage
    assert disk_usage("/root") == (0, 0, 0)


def test_dir_size(jailed):
    (jailed / "a").write_text("x" * 100)
    (jailed / "b").write_text("y" * 50)
    from iitgpu.files import dir_size
    assert dir_size(str(jailed)) == 150


def test_fmt_size():
    from iitgpu.files import fmt_size
    assert fmt_size(512) == "512 B"
    assert "KB" in fmt_size(2048)
    assert "GB" in fmt_size(5 * 1024**3)


def test_delete_image_rejects_non_sif():
    from iitgpu.containers import delete_image
    with patch("iitgpu.containers.in_jail", return_value=True):
        ok, _ = delete_image("/shared/images/x.tar")
    assert ok is False


def test_delete_env_refuses_outside_envs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    import iitgpu.validate as v
    importlib.reload(v)
    from iitgpu.config import load_config
    from iitgpu.envbuilder import delete_env
    bad = tmp_path / "notenvs" / "x"
    bad.mkdir(parents=True)
    ok, msg = delete_env(str(bad), load_config())
    assert ok is False


def test_delete_env_removes_under_envs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("NFS_ROOT", str(tmp_path))
    import iitgpu.validate as v
    importlib.reload(v)
    from iitgpu.config import load_config
    from iitgpu.envbuilder import delete_env
    env = tmp_path / "envs" / "myenv"
    env.mkdir(parents=True)
    ok, _ = delete_env(str(env), load_config())
    assert ok and not env.exists()
