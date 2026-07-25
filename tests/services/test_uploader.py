"""Tests for services/uploader.py — Inbox-based MP3 uploader."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from radio_ripper.domain.models import EnrichedInfo, FingerprintResult
from radio_ripper.infra.config import Settings
from mutagen.id3 import ID3, TXXX

from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.repository import TrackRecord, TrackRepository
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.uploader import Uploader


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "destination": tmp_path / "recordings",
        "database": tmp_path / "ripper.db",
        "mp3_inbox": tmp_path / "mp3_inbox",
    }
    base.update(overrides)
    return Settings.model_validate(base)


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake mp3 content\n")
    return path


def _stub_fingerprint(result: FingerprintResult | None = None, *, fail: type[Exception] | None = None):
    """Return a FingerprintProvider stub."""
    fp = MagicMock(spec=FingerprintProvider)
    if fail is not None:
        fp.fingerprint = AsyncMock(side_effect=fail("fingerprint failed"))
    else:
        fp.fingerprint = AsyncMock(return_value=result)
    return fp


def _stub_metadata(enriched: EnrichedInfo | None = None, *, fail: type[Exception] | None = None):
    """Return a MetadataProvider stub."""
    mp = MagicMock(spec=MetadataProvider)
    if fail is not None:
        mp.fetch = AsyncMock(side_effect=fail("metadata failed"))
    else:
        mp.fetch = AsyncMock(return_value=enriched)
    mp.download_image = AsyncMock(return_value=None)
    return mp


def _stub_tagger() -> TrackTagger:
    t = MagicMock(spec=TrackTagger)
    t.write_full = MagicMock()
    return t


class _TrackingRepo(TrackRepository):
    """In-memory repo that records calls for assertions."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], TrackRecord] = {}
        self.fingerprint_calls: list[tuple[str, str, str, float]] = []
        self.enrichment_calls: list[tuple[str, str, EnrichedInfo]] = []
        self.removed_calls: list[tuple[str, str]] = []
        self.registered: list[tuple[str, str]] = []

    async def exists(self, station_name: str, stream_title: str) -> bool:
        return (station_name, stream_title) in self._records

    async def register(self, track: object, station_name: str) -> None:
        self.registered.append((station_name, str(track)))

    async def update_enrichment(self, station_name: str, stream_title: str, **kwargs: object) -> None:
        import dataclasses

        valid = {f.name for f in dataclasses.fields(EnrichedInfo)}
        self.enrichment_calls.append(
            (
                station_name,
                stream_title,
                EnrichedInfo(**{k: v for k, v in kwargs.items() if k in valid and v is not None}),
            )
        )

    async def update_fingerprint(
        self, station_name: str, stream_title: str, *, recording_id: str, score: float
    ) -> None:
        self.fingerprint_calls.append((station_name, stream_title, recording_id, score))

    async def remove(self, station_name: str, stream_title: str) -> None:
        self.removed_calls.append((station_name, stream_title))

    async def aclose(self) -> None:
        pass

    async def find_all_by_recording_id(self, recording_id: str) -> list[TrackRecord]:
        return []

    async def find_all_by_artist_title(self, artist: str, title: str) -> list[TrackRecord]:
        return []

    async def list_all(self) -> list[TrackRecord]:
        return list(self._records.values())

    async def find_by_file_path(self, file_path: str) -> TrackRecord | None:
        return None

    async def update_file_path(self, station_name: str, stream_title: str, new_path: str) -> None:
        pass


# ---------------------------------------------------------------------------
# Constructor + start / stop
# ---------------------------------------------------------------------------


class TestLifecycle:
    async def test_constructor_stores_deps(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        assert u._inbox == s.mp3_inbox
        assert u._temp_dir == tmp_path / "temp"
        assert u._poll_interval == 60.0

    async def test_start_creates_inbox_dir(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        inbox = tmp_path / "does_not_exist_yet"
        u = Uploader(
            inbox=inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
            poll_interval=0.02,
        )
        try:
            await u.start()
            assert inbox.is_dir()
        finally:
            await u.stop()

    async def test_start_launches_bg_task(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
            poll_interval=0.02,
        )
        try:
            await u.start()
            assert u._task is not None
            assert not u._task.done()
        finally:
            await u.stop()

    async def test_stop_cancels_bg_task(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
            poll_interval=0.02,
        )
        await u.start()
        await u.stop()
        assert u._task is None or u._task.done()

    async def test_double_stop_is_safe(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        await u.start()
        await u.stop()
        await u.stop()  # must not raise


# ---------------------------------------------------------------------------
# _drain_inbox — directory scan
# ---------------------------------------------------------------------------


class TestInboxScan:
    async def test_inbox_missing_is_safe(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=tmp_path / "nonexistent",
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()  # must not raise

    async def test_empty_inbox_is_safe(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        s.mp3_inbox.mkdir(parents=True, exist_ok=True)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()  # must not raise

    async def test_only_mp3_files_processed(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        _touch(s.mp3_inbox / "track.mp3")
        _touch(s.mp3_inbox / "readme.txt")
        _touch(s.mp3_inbox / "song.wav")
        _touch(s.mp3_inbox / "notes.md")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="A", title="B", score=0.9, recording_id="r1"))
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(EnrichedInfo(album="X")),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()
        # Only track.mp3 should have been fingerprinted
        assert len(repo.fingerprint_calls) == 1
        processing_file = s.mp3_inbox / "track.processing"
        assert not processing_file.exists()  # was renamed to destination

    async def test_multi_file_processing_order(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        _touch(s.mp3_inbox / "b.mp3")
        _touch(s.mp3_inbox / "a.mp3")
        _touch(s.mp3_inbox / "c.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(None)  # no match → move to temp
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()
        leftover = list(s.mp3_inbox.iterdir())
        assert len(leftover) == 0  # all processed (moved to temp)


# ---------------------------------------------------------------------------
# _process_one — fingerprint match → route to destination
# ---------------------------------------------------------------------------


class TestProcessOneMatch:
    async def test_match_routes_to_destination(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "track.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(
            FingerprintResult(artist="Artist One", title="Song Two", score=0.95, recording_id="rec-abc")
        )
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        dest = s.destination / "Artist One" / "Artist One - Song Two.mp3"
        assert dest.is_file(), f"Expected {dest} to exist"
        assert not (s.mp3_inbox / "track.processing").exists()
        assert not (s.mp3_inbox / "track.mp3").exists()

    async def test_match_calls_update_fingerprint(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "test.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="A", title="B", score=0.9, recording_id="rec-xyz"))
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert len(repo.fingerprint_calls) == 1
        station, stream_title, rid, score = repo.fingerprint_calls[0]
        assert station == "inbox"
        assert stream_title == "A - B"
        assert rid == "rec-xyz"
        assert score == 0.9

    async def test_match_with_enrichment_also_calls_update_enrichment(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "enriched.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="E", title="F", score=0.95, recording_id="rec-ef"))
        info = EnrichedInfo(album="Great Album", year="2024", genre="Rock", label="Big Label")
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(info),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert len(repo.enrichment_calls) == 1
        station, stream_title, ei = repo.enrichment_calls[0]
        assert station == "inbox"
        assert stream_title == "E - F"
        assert ei.album == "Great Album"
        assert ei.year == "2024"
        # genre is embedded in ID3 tags but not persisted to the DB

    async def test_match_with_album_uses_album_subdir(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "album_track.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(
            FingerprintResult(artist="AlbumArtist", title="AlbumSong", score=0.9, recording_id="r99")
        )
        info = EnrichedInfo(album="MyAlbum")
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(info),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        dest = s.destination / "AlbumArtist" / "MyAlbum" / "AlbumArtist - AlbumSong.mp3"
        assert dest.is_file()

    async def test_match_no_enrichment_data_still_files(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "unenriched.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="U", title="V", score=0.9, recording_id="r-uv"))
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),  # returns None — no enrich data
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        dest = s.destination / "U" / "U - V.mp3"
        assert dest.is_file()

    async def test_match_existing_better_score_skips(self, tmp_path: Path) -> None:
        """Existing file has higher AcoustID score — new one is discarded."""
        s = _settings(tmp_path)
        existing = s.destination / "Artist" / "Artist - Song.mp3"
        _touch(existing)
        # Write a higher score to the existing file
        audio = ID3()
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text="0.98"))
        audio.save(existing, v2_version=3)
        mp3 = _touch(s.mp3_inbox / "collision.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="Artist", title="Song", score=0.95, recording_id="r-c1"))
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert existing.is_file()  # original preserved
        assert not (s.mp3_inbox / "collision.processing").exists()
        assert not (s.mp3_inbox / "collision.mp3").exists()
        # Only the original remains
        files = list((s.destination / "Artist").iterdir())
        assert len(files) == 1

    async def test_match_existing_worse_score_replaces(self, tmp_path: Path) -> None:
        """New file has higher AcoustID score — replaces old file."""
        s = _settings(tmp_path)
        existing = s.destination / "Artist" / "Artist - Song.mp3"
        _touch(existing)
        # Write a lower score to the existing file
        audio = ID3()
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text="0.70"))
        audio.save(existing, v2_version=3)
        mp3 = _touch(s.mp3_inbox / "collision.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="Artist", title="Song", score=0.95, recording_id="r-c1"))
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert existing.is_file()  # still there, but now contains new content
        assert not (s.mp3_inbox / "collision.processing").exists()
        assert not (s.mp3_inbox / "collision.mp3").exists()
        # Only the replaced file remains
        files = list((s.destination / "Artist").iterdir())
        assert len(files) == 1

    async def test_match_artist_title_special_chars_sanitized(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "weird.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(
            FingerprintResult(artist='Art"ist', title="Song: Title?", score=0.9, recording_id="r-spec")
        )
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        # Illegal filename chars removed: " ? →
        dest = s.destination / "Artist" / "Artist - Song Title.mp3"
        assert dest.is_file()


# ---------------------------------------------------------------------------
# _process_one — no match / error paths
# ---------------------------------------------------------------------------


class TestProcessOneNoMatch:
    async def test_no_match_moves_to_temp(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "nobody.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(None)  # no match
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert not (s.mp3_inbox / "nobody.mp3").exists()
        assert not (s.mp3_inbox / "nobody.processing").exists()
        assert (tmp_path / "temp" / "nobody.mp3").is_file()

    async def test_match_recording_id_empty_treated_as_no_match(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "empty_rid.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="A", title="B", score=0.9, recording_id=""))
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert (tmp_path / "temp" / "empty_rid.mp3").is_file()

    async def test_non_retriable_error_deletes_file(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "corrupt.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(fail=NonRetriableFingerprintError)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        # File deleted — not in inbox, not in temp
        assert not (s.mp3_inbox / "corrupt.mp3").exists()
        assert not (s.mp3_inbox / "corrupt.processing").exists()
        assert not (tmp_path / "temp" / "corrupt.processing").exists()

    async def test_fingerprint_error_moves_to_temp(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        _touch(s.mp3_inbox / "net_error.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(fail=FingerprintError)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()
        assert (tmp_path / "temp" / "net_error.mp3").is_file()

    async def test_unexpected_exception_moves_to_temp(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        _touch(s.mp3_inbox / "boom.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(fail=RuntimeError)  # any unexpected error
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()
        assert (tmp_path / "temp" / "boom.mp3").is_file()

    async def test_enrichment_failure_still_files_to_dest(self, tmp_path: Path) -> None:
        """If enrich_and_tag returns None, file still goes to destination (no album subdir)."""
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "partial.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="P", title="Partial", score=0.9, recording_id="r-part"))
        # enrich disabled in settings so enrich_and_tag returns None
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),  # NullMetadataProvider returns None
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        dest = s.destination / "P" / "P - Partial.mp3"
        assert dest.is_file()
        assert len(repo.enrichment_calls) == 1  # pipeline always updates DB state
        _, _, ei = repo.enrichment_calls[0]
        assert ei.album is None  # metadata returned None, no enrichment data

    async def test_rename_failure_skips_gracefully(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = s.mp3_inbox / "locked.mp3"
        _touch(mp3)
        repo = _TrackingRepo()
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
        )
        # Make rename fail by removing the file just before
        mp3.unlink()
        await u._process_one(mp3)  # must not raise
        assert len(repo.fingerprint_calls) == 0


# ---------------------------------------------------------------------------
# temp_dir fallback
# ---------------------------------------------------------------------------


class TestTempDir:
    async def test_temp_dir_created_when_missing(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        mp3 = _touch(s.mp3_inbox / "notemp.mp3")
        temp = tmp_path / "nonexistent_temp_dir"
        fp = _stub_fingerprint(None)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=temp,
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        await u._process_one(mp3)
        assert temp.is_dir()
        assert (temp / "notemp.mp3").is_file()


# ---------------------------------------------------------------------------
# Enrichment error handling
# ---------------------------------------------------------------------------


class TestEnrichmentErrors:
    async def test_enrich_metadata_raises_still_files_to_dest(self, tmp_path: Path) -> None:
        """Enrichment errors are swallowed (consistent with stream recorder).

        The file still goes to destination — enrichment failure must never lose a recording.
        No enrichment call is recorded and no album subdir is created.
        """
        s = _settings(tmp_path)
        _touch(s.mp3_inbox / "bad_meta.mp3")
        repo = _TrackingRepo()
        fp = _stub_fingerprint(FingerprintResult(artist="Meta", title="Fail", score=0.9, recording_id="r-meta"))
        mp = _stub_metadata(fail=RuntimeError)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=mp,
            repository=repo,
            tagger=_stub_tagger(),
        )
        await u._drain_inbox()
        dest = s.destination / "Meta" / "Meta - Fail.mp3"
        assert dest.is_file(), "File must reach destination even when enrichment fails"
        assert len(repo.enrichment_calls) == 1  # pipeline always updates DB state
        _, _, ei = repo.enrichment_calls[0]
        assert ei.album is None  # enrichment failed, no album data


# ---------------------------------------------------------------------------
# Polling loop processes files
# ---------------------------------------------------------------------------


class TestPollingLoop:
    async def test_bg_loop_picks_up_files(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        repo = _TrackingRepo()
        fp = _stub_fingerprint(None)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=fp,
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
            poll_interval=0.02,
        )
        await u.start()
        try:
            _touch(s.mp3_inbox / "poll_test.mp3")
            await asyncio.sleep(0.1)  # let one poll cycle happen
            await asyncio.sleep(0.1)  # second cycle to be safe
            assert not (s.mp3_inbox / "poll_test.mp3").exists()
            assert (tmp_path / "temp" / "poll_test.mp3").is_file()
        finally:
            await u.stop()

    async def test_bg_loop_stops_on_event(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        repo = _TrackingRepo()
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=repo,
            tagger=_stub_tagger(),
            poll_interval=60,  # long interval — we won't wait
        )
        await u.start()
        await u.stop()
        _touch(s.mp3_inbox / "after_stop.mp3")
        # After stop, _drain_inbox should NOT be running
        await asyncio.sleep(0.05)
        assert (s.mp3_inbox / "after_stop.mp3").is_file()


# ---------------------------------------------------------------------------
# Helper internals
# ---------------------------------------------------------------------------


class TestInternalHelpers:
    async def test_cleanup_file_removes_proc_path(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        proc = tmp_path / "gone.processing"
        _touch(proc)
        u._cleanup_file(proc)
        assert not proc.exists()

    async def test_move_to_temp_creates_dir_and_moves(self, tmp_path: Path) -> None:
        s = _settings(tmp_path)
        u = Uploader(
            inbox=s.mp3_inbox,
            temp_dir=tmp_path / "temp",
            settings=s,
            fingerprint_provider=_stub_fingerprint(),
            metadata_provider=_stub_metadata(),
            repository=_TrackingRepo(),
            tagger=_stub_tagger(),
        )
        src = tmp_path / "some.processing"
        _touch(src)
        u._move_to_temp(src)
        assert not src.exists()
        assert (tmp_path / "temp" / "some.mp3").is_file()
