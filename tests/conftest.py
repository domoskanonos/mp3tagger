"""Shared pytest fixtures for radio_ripper tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    """Provide a temporary recordings directory."""
    d = tmp_path / "recordings"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_mp3_bytes(min_size: int = 2048) -> bytes:
    """Create minimal valid-ish MP3 bytes (not playable, just non-empty data)."""
    # Pad with silence MP3 frame header bytes
    return b"\xff\xfb" + b"\x00" * max(0, min_size - 2)


@pytest.fixture
def mp3_bytes() -> bytes:
    return make_mp3_bytes(4096)
