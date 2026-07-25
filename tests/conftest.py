"""Shared pytest fixtures for radio_ripper tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from radio_ripper.services.repository import SQLiteTrackRepository


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> Path:
    """Return path to a fresh in-temp SQLite database file."""
    return tmp_path / "test_ripper.db"


@pytest.fixture
async def sqlite_repo(tmp_db_path: Path) -> SQLiteTrackRepository:
    """Provide a freshly initialised SQLiteTrackRepository and clean up after."""
    repo = SQLiteTrackRepository(tmp_db_path)
    try:
        yield repo
    finally:
        await repo.aclose()


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
