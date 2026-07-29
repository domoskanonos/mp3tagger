"""Tests for radio_ripper.cli (tag)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.cli import main


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
        with patch("radio_ripper.cli.Path.cwd", return_value=tmp_path), \
             patch("radio_ripper.cli._run_pipeline") as mock:
            main([])
        mock.assert_called_once()

    def test_version(self):
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
