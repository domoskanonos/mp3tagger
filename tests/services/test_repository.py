"""Tests for radio_ripper.services.repository."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.errors import RepositoryError
from radio_ripper.services.repository import SQLiteTrackRepository


class TestSQLiteTrackRepository:
    async def test_register_and_exists(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="Adele - Hello",
            artist="Adele",
            title="Hello",
            file_path="/tmp/x.mp3",
            file_size=100,
        )
        await sqlite_repo.register(track, "TopHits")
        assert await sqlite_repo.exists("TopHits", "Adele - Hello")

    async def test_exists_case_insensitive(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="Adele - Hello",
            artist="Adele",
            title="Hello",
            file_path="/tmp/x.mp3",
            file_size=100,
        )
        await sqlite_repo.register(track, "TopHits")
        assert await sqlite_repo.exists("tophits", "adele - hello")

    async def test_exists_returns_false_for_unknown(self, sqlite_repo: SQLiteTrackRepository):
        assert not await sqlite_repo.exists("TopHits", "Unknown - Song")

    async def test_register_is_idempotent(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="A - B",
            artist="A",
            title="B",
            file_path="/x",
            file_size=1,
        )
        await sqlite_repo.register(track, "Rock")
        await sqlite_repo.register(track, "Rock")
        assert await sqlite_repo.exists("Rock", "A - B")

    async def test_different_stations_allow_same_title(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="A - B",
            artist="A",
            title="B",
            file_path="/x",
            file_size=1,
        )
        await sqlite_repo.register(track, "Rock")
        await sqlite_repo.register(track, "Dance")
        assert await sqlite_repo.exists("Rock", "A - B")
        assert await sqlite_repo.exists("Dance", "A - B")

    async def test_update_enrichment(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack(
            stream_title="Adele - Hello",
            artist="Adele",
            title="Hello",
            file_path="/x.mp3",
            file_size=100,
        )
        await sqlite_repo.register(track, "TopHits")
        await sqlite_repo.update_enrichment(
            "TopHits",
            "Adele - Hello",
            album="25",
            year="2015",
            file_size=200,
            has_cover=True,
            enrichment="itunes",
        )
        # Verify by re-registering won't change, but enrichment was updated.
        # We check exists still true
        assert await sqlite_repo.exists("TopHits", "Adele - Hello")

    async def test_update_enrichment_unknown_song_no_error(
        self, sqlite_repo: SQLiteTrackRepository
    ):
        await sqlite_repo.update_enrichment(
            "FakeStation",
            "Unknown - Song",
            album="X",
        )

    async def test_close_releases_connection(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        # Subsequent operations should raise
        with pytest.raises((RepositoryError, sqlite3.ProgrammingError)):
            await repo.exists("x", "y")

    async def test_wal_mode_enabled(self, tmp_db_path: Path):
        SQLiteTrackRepository(tmp_db_path)
        # WAL file created on first write
        track = SavedTrack("A - B", "A", "B", "/x", 1)
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.register(track, "S")
        await repo.aclose()
        # Check journal mode
        import sqlite3

        conn = sqlite3.connect(str(tmp_db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    async def test_remove_deletes_record(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack("A - B", "A", "B", "/x", 1)
        await sqlite_repo.register(track, "Rock")
        assert await sqlite_repo.exists("Rock", "A - B")
        await sqlite_repo.remove("Rock", "A - B")
        assert not await sqlite_repo.exists("Rock", "A - B")

    async def test_update_fingerprint(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack("A - B", "A", "B", "/x", 1)
        await sqlite_repo.register(track, "Rock")
        await sqlite_repo.update_fingerprint("Rock", "A - B", recording_id="abc123", score=0.95)
        all_by_id = await sqlite_repo.find_all_by_recording_id("abc123")
        assert len(all_by_id) == 1
        assert all_by_id[0].station_name == "Rock"
        assert all_by_id[0].track.stream_title == "A - B"
        assert all_by_id[0].track.acoustid_score == 0.95

    async def test_find_all_by_artist_title(self, sqlite_repo: SQLiteTrackRepository):
        t1 = SavedTrack("A - X", "A", "X", "/a.mp3", 100)
        t2 = SavedTrack("A - X", "A", "X", "/a2.mp3", 200)
        await sqlite_repo.register(t1, "Rock")
        await sqlite_repo.register(t2, "Dance")
        results = await sqlite_repo.find_all_by_artist_title("A", "X")
        assert len(results) == 2
        assert results[0].station_name == "Rock"
        assert results[1].station_name == "Dance"

    async def test_find_all_by_artist_title_case_insensitive(
        self, sqlite_repo: SQLiteTrackRepository
    ):
        track = SavedTrack("A - X", "A", "X", "/a.mp3", 100)
        await sqlite_repo.register(track, "Rock")
        results = await sqlite_repo.find_all_by_artist_title("a", "x")
        assert len(results) == 1

    async def test_find_all_by_artist_title_no_match(self, sqlite_repo: SQLiteTrackRepository):
        results = await sqlite_repo.find_all_by_artist_title("Nobody", "Nothing")
        assert results == []

    async def test_find_by_file_path(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack("A - B", "A", "B", "/specific/path.mp3", 100)
        await sqlite_repo.register(track, "Rock")
        found = await sqlite_repo.find_by_file_path("/specific/path.mp3")
        assert found is not None
        assert found.station_name == "Rock"
        assert found.track.stream_title == "A - B"

    async def test_find_by_file_path_not_found(self, sqlite_repo: SQLiteTrackRepository):
        found = await sqlite_repo.find_by_file_path("/nonexistent.mp3")
        assert found is None

    async def test_list_all(self, sqlite_repo: SQLiteTrackRepository):
        t1 = SavedTrack("A - X", "A", "X", "/a.mp3", 100)
        t2 = SavedTrack("B - Y", "B", "Y", "/b.mp3", 200)
        await sqlite_repo.register(t1, "Rock")
        await sqlite_repo.register(t2, "Dance")
        all_tracks = await sqlite_repo.list_all()
        assert len(all_tracks) == 2

    async def test_list_all_empty(self, sqlite_repo: SQLiteTrackRepository):
        all_tracks = await sqlite_repo.list_all()
        assert all_tracks == []

    async def test_update_file_path(self, sqlite_repo: SQLiteTrackRepository):
        track = SavedTrack("A - B", "A", "B", "/old.mp3", 100)
        await sqlite_repo.register(track, "Rock")
        await sqlite_repo.update_file_path("Rock", "A - B", "/new.mp3")
        found = await sqlite_repo.find_by_file_path("/new.mp3")
        assert found is not None
        assert found.track.file_path == "/new.mp3"

    async def test_run_static_method(self):
        assert SQLiteTrackRepository._run(42) == 42

    async def test_register_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        track = SavedTrack("A - B", "A", "B", "/x", 1)
        with pytest.raises(RepositoryError, match="register"):
            await repo.register(track, "Rock")

    async def test_update_enrichment_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="update_enrichment"):
            await repo.update_enrichment("S", "T", album="X")

    async def test_update_fingerprint_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="update_fingerprint"):
            await repo.update_fingerprint("S", "T", recording_id="x", score=0.5)

    async def test_find_all_by_recording_id_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="find_all_by_recording_id"):
            await repo.find_all_by_recording_id("x")

    async def test_find_all_by_artist_title_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="find_all_by_artist_title"):
            await repo.find_all_by_artist_title("A", "B")

    async def test_find_by_file_path_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="find_by_file_path"):
            await repo.find_by_file_path("/x.mp3")

    async def test_list_all_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="list_all"):
            await repo.list_all()

    async def test_update_file_path_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="update_file_path"):
            await repo.update_file_path("S", "T", "/new.mp3")

    async def test_remove_on_closed_db_raises(self, tmp_db_path: Path):
        repo = SQLiteTrackRepository(tmp_db_path)
        await repo.aclose()
        with pytest.raises(RepositoryError, match="remove"):
            await repo.remove("S", "T")
