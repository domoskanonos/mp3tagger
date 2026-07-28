"""Tests for :mod:`radio_ripper.infra.catalog`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.infra.catalog import (
    SongRecord,
    SqliteCatalog,
    read_audio_from_file,
    read_tags_from_file,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_record(
    file_path: str = "/dest/Artist/Artist - Title.mp3",
    recording_id: str | None = "mbid-1",
    isrc: str | None = "ISRC001",
    artist: str = "Artist",
    title: str = "Title",
    bitrate: int | None = 320,
    sample_rate: int | None = 44100,
    duration_ms: int | None = 200000,
    acoustid_score: float | None = 0.95,
    popularity_rank: int | None = 50000,
) -> SongRecord:
    return SongRecord(
        file_path=file_path, recording_id=recording_id, isrc=isrc,
        artist=artist, title=title, bitrate=bitrate, sample_rate=sample_rate,
        duration_ms=duration_ms, acoustid_score=acoustid_score,
        popularity_rank=popularity_rank,
    )


@pytest.fixture
async def catalog(tmp_path: Path) -> SqliteCatalog:
    cat = SqliteCatalog(tmp_path / "catalog.db")
    yield cat
    await cat.aclose()


# ── Schema & Migrations ──────────────────────────────────────────────────────


class TestSchema:
    async def test_creates_schema_on_init(self, tmp_path: Path):
        cat = SqliteCatalog(tmp_path / "catalog.db")
        assert (tmp_path / "catalog.db").is_file()
        rows = cat._conn.execute("PRAGMA table_info(songs)").fetchall()
        col_names = {r[1] for r in rows}
        assert "recording_id" in col_names
        assert "isrc" in col_names
        assert "bitrate" in col_names
        assert "popularity_rank" in col_names
        await cat.aclose()

    async def test_migration_columns_idempotent(self, tmp_path: Path):
        cat1 = SqliteCatalog(tmp_path / "catalog.db")
        await cat1.aclose()
        cat2 = SqliteCatalog(tmp_path / "catalog.db")
        await cat2.aclose()


# ── upsert ────────────────────────────────────────────────────────────────────


class TestUpsert:
    async def test_insert_new_record(self, catalog: SqliteCatalog):
        rec = _make_record()
        await catalog.upsert(rec)
        assert await catalog.count() == 1
        assert await catalog.exists_by_path(rec.file_path)

    async def test_upsert_updates_existing(self, catalog: SqliteCatalog):
        rec = _make_record()
        await catalog.upsert(rec)
        updated = _make_record(file_path=rec.file_path, bitrate=256, popularity_rank=99999)
        await catalog.upsert(updated)
        assert await catalog.count() == 1
        rows = await catalog.list_all()
        assert rows[0].bitrate == 256
        assert rows[0].popularity_rank == 99999

    async def test_multiple_distinct_paths_inserted(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a/x.mp3"))
        await catalog.upsert(_make_record(file_path="/b/y.mp3"))
        assert await catalog.count() == 2


# ── find_by_recording_id ────────────────────────────────────────────────────


class TestFindByRecordingId:
    async def test_empty_when_no_match(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(recording_id="mbid-1"))
        assert await catalog.find_by_recording_id("mbid-999") == []

    async def test_finds_all_versions(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", recording_id="mbid-1", acoustid_score=0.85))
        await catalog.upsert(_make_record(file_path="/b.mp3", recording_id="mbid-1", acoustid_score=0.95))
        await catalog.upsert(_make_record(file_path="/c.mp3", recording_id="mbid-2"))
        results = await catalog.find_by_recording_id("mbid-1")
        assert len(results) == 2
        assert results[0].acoustid_score == 0.95  # DESC sortiert
        assert results[1].acoustid_score == 0.85


# ── find_duplicate_versions ───────────────────────────────────────────────────


class TestFindDuplicateVersions:
    async def test_no_duplicates_when_unique(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", recording_id="mb1", isrc="i1"))
        await catalog.upsert(_make_record(file_path="/b.mp3", recording_id="mb2", isrc="i2"))
        assert await catalog.find_duplicate_versions() == []

    async def test_finds_duplicates_by_recording_id_and_isrc(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", recording_id="mb1", isrc="i1", acoustid_score=0.85))
        await catalog.upsert(_make_record(file_path="/b.mp3", recording_id="mb1", isrc="i1", acoustid_score=0.95))
        groups = await catalog.find_duplicate_versions()
        assert len(groups) == 1
        assert len(groups[0]) == 2
        assert groups[0][0].acoustid_score == 0.95

    async def test_ignores_without_isrc(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", recording_id="mb1", isrc=None))
        await catalog.upsert(_make_record(file_path="/b.mp3", recording_id="mb1", isrc=None))
        assert await catalog.find_duplicate_versions() == []

    async def test_ignores_without_recording_id(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", recording_id=None, isrc="i1"))
        await catalog.upsert(_make_record(file_path="/b.mp3", recording_id=None, isrc="i1"))
        assert await catalog.find_duplicate_versions() == []

    async def test_same_mb_different_isrc_no_dup(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", recording_id="mb1", isrc="i1"))
        await catalog.upsert(_make_record(file_path="/b.mp3", recording_id="mb1", isrc="i2"))
        assert await catalog.find_duplicate_versions() == []


# ── find_least_popular ────────────────────────────────────────────────────────


class TestFindLeastPopular:
    async def test_empty_when_no_ranked(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", popularity_rank=None))
        assert await catalog.find_least_popular() == []

    async def test_sorted_by_popularity_asc(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3", popularity_rank=50000))
        await catalog.upsert(_make_record(file_path="/b.mp3", popularity_rank=10000))
        await catalog.upsert(_make_record(file_path="/c.mp3", popularity_rank=99999))
        results = await catalog.find_least_popular(limit=10)
        assert [r.popularity_rank for r in results] == [10000, 50000, 99999]

    async def test_respects_limit(self, catalog: SqliteCatalog):
        for i in range(5):
            await catalog.upsert(_make_record(file_path=f"/{i}.mp3", popularity_rank=i * 1000))
        results = await catalog.find_least_popular(limit=2)
        assert len(results) == 2


# ── count, remove, exists_by_path ────────────────────────────────────────────


class TestCountRemoveExists:
    async def test_count_zero_on_empty(self, catalog: SqliteCatalog):
        assert await catalog.count() == 0

    async def test_count_after_inserts(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3"))
        await catalog.upsert(_make_record(file_path="/b.mp3"))
        assert await catalog.count() == 2

    async def test_remove_deletes_entry(self, catalog: SqliteCatalog):
        await catalog.upsert(_make_record(file_path="/a.mp3"))
        await catalog.remove("/a.mp3")
        assert await catalog.exists_by_path("/a.mp3") is False
        assert await catalog.count() == 0

    async def test_remove_nonexistent_is_noop(self, catalog: SqliteCatalog):
        await catalog.remove("/nonexistent")
        assert await catalog.count() == 0


# ── reconcile_with_filesystem ────────────────────────────────────────────────


class TestReconcile:
    async def test_empty_destination_returns_empty_report(self, catalog: SqliteCatalog, tmp_path: Path):
        dest = tmp_path / "dest"
        dest.mkdir()
        report = await catalog.reconcile_with_filesystem(dest)
        assert report.added == 0 and report.removed == 0 and report.kept == 0

    async def test_removes_orphaned_db_entries(self, catalog: SqliteCatalog, tmp_path: Path):
        await catalog.upsert(_make_record(file_path=str(tmp_path / "ghost.mp3")))
        dest = tmp_path / "dest"
        dest.mkdir()
        report = await catalog.reconcile_with_filesystem(dest)
        assert report.removed == 1
        assert report.kept == 0
        assert await catalog.count() == 0

    async def test_keeps_existing_entries_with_file_present(self, catalog: SqliteCatalog, tmp_path: Path):
        f = tmp_path / "dest" / "x.mp3"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")
        await catalog.upsert(_make_record(file_path=str(f)))
        report = await catalog.reconcile_with_filesystem(tmp_path / "dest")
        assert report.kept == 1
        assert report.added == 0
        assert await catalog.count() == 1

    async def test_indexes_new_mp3_files(self, catalog: SqliteCatalog, tmp_path: Path):
        dest = tmp_path / "dest"
        f = dest / "Artist" / "Artist - Song.mp3"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")
        # Stubs: read_tags_from_file + read_audio_from_file liefern deterministische Werte
        with (
            patch("radio_ripper.infra.catalog.read_tags_from_file", return_value={
                "recording_id": "mbid-1", "isrc": "ISRC1",
                "release_group_type": "Album", "artist": "Artist", "title": "Song",
                "album": None, "acoustid_score": 0.9, "popularity_rank": 12345,
                "has_cover": True,
            }),
            patch("radio_ripper.infra.catalog.read_audio_from_file", return_value={
                "bitrate": 320, "sample_rate": 44100, "duration_ms": 200000,
            }),
        ):
            report = await catalog.reconcile_with_filesystem(dest)
        assert report.added == 1
        rows = await catalog.list_all()
        assert len(rows) == 1
        r = rows[0]
        assert r.recording_id == "mbid-1" and r.isrc == "ISRC1"
        assert r.bitrate == 320 and r.sample_rate == 44100
        assert r.popularity_rank == 12345

    async def test_records_error_for_unreadable_file(self, catalog: SqliteCatalog, tmp_path: Path):
        dest = tmp_path / "dest"
        f = dest / "bad.mp3"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"")
        with patch(
            "radio_ripper.infra.catalog.read_tags_from_file",
            side_effect=RuntimeError("boom"),
        ):
            report = await catalog.reconcile_with_filesystem(dest)
        assert report.added == 0
        assert len(report.errors) == 1
        assert "boom" in report.errors[0]


# ── read_tags_from_file / read_audio_from_file ───────────────────────────────


class TestReadHelpers:
    def test_read_audio_returns_none_on_missing_file(self, tmp_path: Path):
        result = read_audio_from_file(tmp_path / "nonexistent.mp3")
        assert result == {"bitrate": None, "sample_rate": None, "duration_ms": None}

    def test_read_tags_missing_file_returns_none_dict(self, tmp_path: Path):
        result = read_tags_from_file(tmp_path / "nonexistent.mp3")
        assert result["recording_id"] is None
        assert result["isrc"] is None
        assert result["has_cover"] is False
