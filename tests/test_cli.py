"""Tests for radio_ripper.cli (tag)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.cli import _requeue_failed_files, main

_LOGGER = logging.getLogger("test_cli")
_LOGGER.addHandler(logging.NullHandler())


class TestCli:
    def test_help(self):
        with pytest.raises(SystemExit) as exc:
            main(["--help"])
        assert exc.value.code == 0

    def test_minimal_config_path(self, tmp_path: Path):
        cfg = tmp_path / "cfg.json"
        cfg.write_text('{"destination":"' + str(tmp_path / "rec") + '"}')
        with patch("radio_ripper.cli._run_pipeline") as mock:
            main(["--config", str(cfg)])
        mock.assert_called_once()

    def test_missing_config_falls_back_to_defaults(self):
        rc = main(["--config", "/nonexistent/config.json"])
        assert rc == 1  # fails on missing ACOUSTID_API_KEY, not on missing config

    def test_auto_discover_config_json(self, tmp_path: Path):
        cfg = tmp_path / "config.json"
        cfg.write_text('{"destination":"' + str(tmp_path / "rec") + '"}')
        with patch("radio_ripper.cli.Path.cwd", return_value=tmp_path), patch("radio_ripper.cli._run_pipeline") as mock:
            main([])
        mock.assert_called_once()

    def test_version(self):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0


class TestRequeueFailedFiles:
    def test_moves_mp3s_back_to_inbox(self, tmp_path: Path):
        failed = tmp_path / "failed"
        failed.mkdir()
        (failed / "song.mp3").write_bytes(b"data")
        (failed / "other.mp3").write_bytes(b"data")
        inbox = tmp_path / "inbox"

        count = _requeue_failed_files(failed, inbox, _LOGGER)

        assert count == 2
        assert (inbox / "song.mp3").exists()
        assert (inbox / "other.mp3").exists()
        assert not (failed / "song.mp3").exists()

    def test_skips_when_target_exists(self, tmp_path: Path):
        failed = tmp_path / "failed"
        failed.mkdir()
        (failed / "song.mp3").write_bytes(b"data")
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "song.mp3").write_bytes(b"other")

        count = _requeue_failed_files(failed, inbox, _LOGGER)

        assert count == 0
        assert (failed / "song.mp3").exists()  # bleibt liegen, kein Überschreiben

    def test_missing_failed_dir_is_noop(self, tmp_path: Path):
        count = _requeue_failed_files(tmp_path / "nope", tmp_path / "inbox", _LOGGER)
        assert count == 0
