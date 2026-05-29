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
