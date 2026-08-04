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

    def test_jsonc_comments_are_ignored(self, tmp_path: Path):
        text = '// ein Kommentar\n{"destination": "./rec"} // trailing'
        p = tmp_path / "config.jsonc"
        p.write_text(text, encoding="utf-8")
        s = load_settings(p)
        assert isinstance(s, Settings)

    def test_invalid_log_level(self, tmp_path: Path):
        cfg = dict(GOOD_BASE, log_level="INVALID")
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)

    def test_work_paths_are_resolved(self, tmp_path: Path):
        path = _write_config(tmp_path, {"destination": str(tmp_path / "rec")})
        s = load_settings(path)
        assert s.log_file is not None

    def test_log_level_overrides(self, tmp_path: Path):
        cfg = dict(GOOD_BASE, log_level="DEBUG")
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.log_level == "DEBUG"


class TestCollectionManagementDefaults:
    def test_defaults_apply(self, tmp_path: Path):
        path = _write_config(tmp_path, GOOD_BASE)
        s = load_settings(path)
        assert s.reconcile_on_startup is True
        assert s.max_collection_size == 0
        assert s.enable_eviction is False

    def test_collection_fields_parsed(self, tmp_path: Path):
        cfg = dict(
            GOOD_BASE,
            reconcile_on_startup=False,
            max_collection_size=10000,
            enable_eviction=True,
        )
        path = _write_config(tmp_path, cfg)
        s = load_settings(path)
        assert s.reconcile_on_startup is False
        assert s.max_collection_size == 10000
        assert s.enable_eviction is True

    def test_invalid_max_collection_size(self, tmp_path: Path):
        cfg = dict(GOOD_BASE, max_collection_size=-1)
        path = _write_config(tmp_path, cfg)
        with pytest.raises(ConfigurationError):
            load_settings(path)
