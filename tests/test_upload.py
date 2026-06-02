import os
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

from iitgpu.upload import (
    _validate_folder_name,
    _ensure_folder,
    _download_from_url,
    _browse_folder,
    _show_scp_instructions,
)


# ---------------------------------------------------------------------------
# _validate_folder_name
# ---------------------------------------------------------------------------

class TestValidateFolderName:
    def test_simple_name(self):
        assert _validate_folder_name("mydata") is True

    def test_hyphens_and_underscores(self):
        assert _validate_folder_name("training-set_v2") is True

    def test_starts_with_digit(self):
        assert _validate_folder_name("2024data") is True

    def test_max_length(self):
        assert _validate_folder_name("a" * 64) is True

    def test_too_long(self):
        assert _validate_folder_name("a" * 65) is False

    def test_empty_string(self):
        assert _validate_folder_name("") is False

    def test_slash_rejected(self):
        assert _validate_folder_name("my/data") is False

    def test_dot_dot_rejected(self):
        assert _validate_folder_name("../escape") is False

    def test_space_rejected(self):
        assert _validate_folder_name("my data") is False

    def test_special_chars_rejected(self):
        assert _validate_folder_name("data;rm") is False


# ---------------------------------------------------------------------------
# _ensure_folder
# ---------------------------------------------------------------------------

class TestEnsureFolder:
    def test_creates_new_dir(self, tmp_path):
        target = str(tmp_path / "newdir")
        assert _ensure_folder(target) is True
        assert Path(target).is_dir()

    def test_accepts_existing_dir(self, tmp_path):
        assert _ensure_folder(str(tmp_path)) is True

    def test_nested_creation(self, tmp_path):
        target = str(tmp_path / "a" / "b" / "c")
        assert _ensure_folder(target) is True
        assert Path(target).is_dir()


# ---------------------------------------------------------------------------
# _download_from_url — security checks
# ---------------------------------------------------------------------------

class TestDownloadFromUrl:
    def _make_text_mock(self, value):
        m = MagicMock()
        m.ask.return_value = value
        return m

    def test_rejects_ftp_url(self, capsys):
        with patch("questionary.text", return_value=self._make_text_mock("ftp://evil.com/file")), \
             patch("iitgpu.upload.header"):
            _download_from_url("/shared/folder")
        out = capsys.readouterr().out
        assert "https" in out or "http" in out  # err message shown

    def test_rejects_empty_input(self):
        with patch("questionary.text", return_value=self._make_text_mock("")), \
             patch("iitgpu.upload.header"):
            _download_from_url("/shared/folder")  # should return without subprocess

    def test_rejects_path_outside_jail(self):
        with patch("questionary.text", return_value=self._make_text_mock("https://example.com/file.tar")), \
             patch("iitgpu.upload.header"), \
             patch("iitgpu.upload.in_jail", return_value=False), \
             patch("iitgpu.upload.auditclient"), \
             patch("subprocess.run") as mock_run:
            _download_from_url("/shared/folder")
            mock_run.assert_not_called()

    def test_logs_url_to_audit(self):
        with patch("questionary.text", return_value=self._make_text_mock("https://example.com/data.zip")), \
             patch("iitgpu.upload.header"), \
             patch("iitgpu.upload.in_jail", return_value=True), \
             patch("iitgpu.upload.auditclient") as mock_audit, \
             patch("subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("iitgpu.upload.info"), \
             patch("iitgpu.upload.ok"), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=1024)), \
             patch("pathlib.Path.exists", return_value=True):
            _download_from_url("/shared/folder")
            mock_audit.log.assert_called_once_with(
                "data_download_url", detail="https://example.com/data.zip"
            )

    def test_never_uses_shell_true(self):
        calls_seen = []

        def fake_run(cmd, **kwargs):
            calls_seen.append((cmd, kwargs))
            return MagicMock(returncode=0)

        with patch("questionary.text", return_value=self._make_text_mock("https://example.com/data.tar")), \
             patch("iitgpu.upload.header"), \
             patch("iitgpu.upload.in_jail", return_value=True), \
             patch("iitgpu.upload.auditclient"), \
             patch("iitgpu.upload.info"), \
             patch("iitgpu.upload.ok"), \
             patch("subprocess.run", side_effect=fake_run), \
             patch("pathlib.Path.stat", return_value=MagicMock(st_size=0)), \
             patch("pathlib.Path.exists", return_value=True):
            _download_from_url("/shared/folder")

        for _cmd, kwargs in calls_seen:
            assert kwargs.get("shell") is not True, "shell=True must never be used"


# ---------------------------------------------------------------------------
# _browse_folder
# ---------------------------------------------------------------------------

class TestBrowseFolder:
    def test_empty_folder(self, tmp_path, capsys):
        with patch("questionary.press_any_key_to_continue") as mock_key:
            mock_key.return_value.ask.return_value = None
            with patch("iitgpu.upload.header"):
                _browse_folder(str(tmp_path))
        out = capsys.readouterr().out
        assert "empty" in out.lower()

    def test_lists_files(self, tmp_path, capsys):
        (tmp_path / "train.csv").write_text("a,b")
        (tmp_path / "subdir").mkdir()
        with patch("questionary.press_any_key_to_continue") as mock_key:
            mock_key.return_value.ask.return_value = None
            with patch("iitgpu.upload.header"):
                _browse_folder(str(tmp_path))
        out = capsys.readouterr().out
        assert "train.csv" in out
        assert "subdir" in out


# ---------------------------------------------------------------------------
# run_upload — folder selection flow
# ---------------------------------------------------------------------------

class TestRunUpload:
    def _sel(self, value):
        m = MagicMock()
        m.ask.return_value = value
        return m

    def test_cancel_exits_without_creating_anything(self, tmp_path, monkeypatch):
        """Selecting [cancel] returns without creating any folder."""
        monkeypatch.setenv("NFS_ROOT", str(tmp_path))
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

        monkeypatch.setattr(
            "questionary.select",
            lambda *a, **kw: self._sel("__cancel__"),
        )

        from iitgpu.upload import run_upload
        run_upload()
        # Nothing created inside tmp_path
        assert list(tmp_path.iterdir()) == []

    def test_select_existing_folder_uses_it_directly(self, tmp_path, monkeypatch):
        """Picking an existing folder skips the name-entry prompt entirely."""
        monkeypatch.setenv("NFS_ROOT", str(tmp_path))
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        monkeypatch.setattr("iitgpu.auditclient.log", lambda *a, **kw: None)

        existing = tmp_path / "my-dataset"
        existing.mkdir()

        # First select: pick the existing folder path; second: "back"
        sel_responses = iter([str(existing), "back"])
        monkeypatch.setattr(
            "questionary.select",
            lambda *a, **kw: self._sel(next(sel_responses)),
        )

        from iitgpu.upload import run_upload
        run_upload()

        assert existing.is_dir()
        # No new folder should have been created
        assert list(tmp_path.iterdir()) == [existing]

    def test_create_new_folder_prompts_for_name_and_creates_dir(self, tmp_path, monkeypatch):
        """Choosing [create new folder] prompts for a name and creates the directory."""
        monkeypatch.setenv("NFS_ROOT", str(tmp_path))
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        monkeypatch.setattr("iitgpu.auditclient.log", lambda *a, **kw: None)

        # First select: "create new"; second select: "back"
        sel_responses = iter(["__new__", "back"])
        monkeypatch.setattr(
            "questionary.select",
            lambda *a, **kw: self._sel(next(sel_responses)),
        )
        monkeypatch.setattr(
            "questionary.text",
            lambda *a, **kw: self._sel("newdataset"),
        )

        from iitgpu.upload import run_upload
        run_upload()

        assert (tmp_path / "newdataset").is_dir()

    def test_existing_folders_appear_in_choices(self, tmp_path, monkeypatch):
        """Existing subdirectories of nfs_root are listed as selectable choices."""
        monkeypatch.setenv("NFS_ROOT", str(tmp_path))
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

        (tmp_path / "alpha").mkdir()
        (tmp_path / "beta").mkdir()

        choices_seen: list = []

        def fake_select(prompt, choices, **kw):
            choices_seen.extend(
                c.value if hasattr(c, "value") else c for c in choices
            )
            m = MagicMock()
            m.ask.return_value = "__cancel__"
            return m

        monkeypatch.setattr("questionary.select", fake_select)

        from iitgpu.upload import run_upload
        run_upload()

        assert str(tmp_path / "alpha") in choices_seen
        assert str(tmp_path / "beta") in choices_seen
        assert "__new__" in choices_seen
        assert "__cancel__" in choices_seen

    def test_no_existing_folders_still_offers_create(self, tmp_path, monkeypatch):
        """With an empty nfs_root, the prompt still offers [create new folder]."""
        monkeypatch.setenv("NFS_ROOT", str(tmp_path))
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

        choices_seen: list = []

        def fake_select(prompt, choices, **kw):
            choices_seen.extend(
                c.value if hasattr(c, "value") else c for c in choices
            )
            m = MagicMock()
            m.ask.return_value = "__cancel__"
            return m

        monkeypatch.setattr("questionary.select", fake_select)

        from iitgpu.upload import run_upload
        run_upload()

        assert "__new__" in choices_seen
        assert "__cancel__" in choices_seen

    def test_audit_log_called_on_folder_open(self, tmp_path, monkeypatch):
        """run_upload logs data_folder_open after the folder is ready."""
        monkeypatch.setenv("NFS_ROOT", str(tmp_path))
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")

        logged = []
        monkeypatch.setattr(
            "iitgpu.auditclient.log",
            lambda action, **kw: logged.append((action, kw)),
        )

        existing = tmp_path / "my-dataset"
        existing.mkdir()

        sel_responses = iter([str(existing), "back"])
        monkeypatch.setattr(
            "questionary.select",
            lambda *a, **kw: self._sel(next(sel_responses)),
        )

        from iitgpu.upload import run_upload
        run_upload()

        assert any(action == "data_folder_open" for action, _ in logged)


# ---------------------------------------------------------------------------
# _show_scp_instructions — gateway host/port and quoted paths
# ---------------------------------------------------------------------------

class TestShowScpInstructions:
    def _make_cfg(self, gateway_host="10.35.4.100", gateway_port="2225"):
        import dataclasses
        from iitgpu.config import load_config
        cfg = load_config()
        return dataclasses.replace(cfg, gateway_host=gateway_host, gateway_port=gateway_port)

    def test_uses_gateway_host_not_socket_hostname(self, capsys, monkeypatch):
        """Output must reference cfg.gateway_host, not socket.gethostname()."""
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        cfg = self._make_cfg(gateway_host="10.35.4.100", gateway_port="2225")

        with patch("questionary.press_any_key_to_continue") as mock_key, \
             patch("iitgpu.upload.header"):
            mock_key.return_value.ask.return_value = None
            _show_scp_instructions("/shared/mydata", cfg)

        out = capsys.readouterr().out
        assert "10.35.4.100" in out
        assert "login-node" not in out

    def test_scp_uses_uppercase_P_for_port(self, capsys, monkeypatch):
        """scp port flag must be -P (uppercase), not -p."""
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        cfg = self._make_cfg(gateway_port="2225")

        with patch("questionary.press_any_key_to_continue") as mock_key, \
             patch("iitgpu.upload.header"):
            mock_key.return_value.ask.return_value = None
            _show_scp_instructions("/shared/mydata", cfg)

        out = capsys.readouterr().out
        assert "-P 2225" in out

    def test_rsync_uses_ssh_p_for_port(self, capsys, monkeypatch):
        """rsync port is passed via -e 'ssh -p PORT'."""
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        cfg = self._make_cfg(gateway_port="2225")

        with patch("questionary.press_any_key_to_continue") as mock_key, \
             patch("iitgpu.upload.header"):
            mock_key.return_value.ask.return_value = None
            _show_scp_instructions("/shared/mydata", cfg)

        out = capsys.readouterr().out
        assert "ssh -p 2225" in out

    def test_folder_path_is_quoted(self, capsys, monkeypatch):
        """Remote path must be wrapped in double-quotes to handle spaces."""
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        cfg = self._make_cfg()

        with patch("questionary.press_any_key_to_continue") as mock_key, \
             patch("iitgpu.upload.header"):
            mock_key.return_value.ask.return_value = None
            _show_scp_instructions("/shared/my folder", cfg)

        out = capsys.readouterr().out
        assert '"/shared/my folder/' in out

    def test_data_ref_path_is_quoted(self, capsys, monkeypatch):
        """The '--data ...' reference line must also quote the path."""
        monkeypatch.setenv("IIT_SITE_ENV", "/nonexistent")
        cfg = self._make_cfg()

        with patch("questionary.press_any_key_to_continue") as mock_key, \
             patch("iitgpu.upload.header"):
            mock_key.return_value.ask.return_value = None
            _show_scp_instructions("/shared/my folder", cfg)

        out = capsys.readouterr().out
        assert '--data "/shared/my folder"' in out
