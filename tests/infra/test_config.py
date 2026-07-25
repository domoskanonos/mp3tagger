"""Tests for radio_ripper.infra.config (tag)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from radio_ripper.infra.config import Settings, load_settings
from radio_ripper.infra.errors import ConfigurationError


def _write_config(tmp_path: Path, payload: dict | str) -> Path:
    p = tmp_path / "config.json"
    text = payload if isinstance(payload, str) else json.dumps(payload)
    p.write_text(text, encoding="utf-8")
    return p


GOOD_BASE = {
    "destination": "./recordings",
    "database": "./recordings/ripper.db",
}


class TestLoadSettings:
    def test_load_good_config(self, tmp_path: Path):
        path = _write_config(tmp_path, GOOD_BASE)
        s = load_settings(path)
        assert isinstance(s, Settings)

    def test_load_minimal_config(self, tmp_path: Path):
        cfg = {"destination": str(tmp_path / "rec")}
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.destination == tmp_path / "rec"

    def test_file_not_found(self, tmp_path: Path):
        with pytest.raises(ConfigurationError):
            load_settings(tmp_path / "nonexistent.json")

    def test_invalid_json(self, tmp_path: Path):
        path = _write_config(tmp_path, "{broken")
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_invalid_log_level(self, tmp_path: Path):
        cfg = dict(GOOD_BASE, log_level="INVALID")
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_work_paths_are_resolved(self, tmp_path: Path):
        cfg = dict(GOOD_BASE)
        del cfg["database"]
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.database is not None
        assert s.log_file is not None

    def test_log_level_overrides(self, tmp_path: Path):
        cfg = dict(GOOD_BASE, log_level="DEBUG")
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.log_level == "DEBUG"
