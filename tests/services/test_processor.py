"""Tests for radio_ripper.services.processor."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from radio_ripper.domain.models import (
    EnrichedInfo,
    FingerprintResult,
    MusicBrainzData,
    TrackInfo,
)
from radio_ripper.infra.catalog import read_tags_from_file
from radio_ripper.infra.config import Settings
from radio_ripper.services.fingerprint import (
    FingerprintError,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata_deezer import DeezerData
from radio_ripper.services.processor import (
    CollectedMetadata,
    FileProcessor,
    _fetch_artist_image,
    _fetch_cover_data,
    _fetch_lyrics,
    _strip_untested_suffix,
    correct_fingerprint_result,
)

# ── Fixtures ──


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        destination=tmp_path / "out",
        work_dir=tmp_path / "work",
        source=tmp_path / "inbox",
        min_popularity_rank=0,
    )


def _make_processor(tmp_path: Path, **overrides: Any) -> FileProcessor:
    settings = _settings(tmp_path)
    for key, val in overrides.items():
        settings = settings.model_copy(update={key: val})
    fp = AsyncMock()
    meta = AsyncMock()
    tagger = MagicMock()
    inbox = settings.source
    assert inbox is not None
    return FileProcessor(
        inbox=inbox,
        temp_dir=settings.work_dir / "failed",
        settings=settings,
        fingerprint_provider=fp,
        metadata_provider=meta,
        tagger=tagger,
        name="tag",
        cover_provider=overrides.get("cover_provider"),
        popularity_provider=overrides.get("popularity_provider"),
        lyrics_provider=overrides.get("lyrics_provider"),
        logger=logging.getLogger("test"),
    )


def _write_mp3(path: Path, size: int = 128) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb" + b"\x00" * (size - 2))


# ── _strip_untested_suffix ──


class TestStripUntestedSuffix:
    def test_no_untested_suffix_unchanged(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_mp3(f)
        result = _strip_untested_suffix(f, logging.getLogger("t"), "station")
        assert result == f

    def test_removes_untested_suffix(self, tmp_path: Path):
        f = tmp_path / "song.untested.mp3"
        _write_mp3(f)
        result = _strip_untested_suffix(f, logging.getLogger("t"), "station")
        assert result is not None
        assert result.name == "song.mp3"
        assert result.exists()
        assert not f.exists()

    def test_target_exists_returns_none(self, tmp_path: Path):
        f = tmp_path / "song.untested.mp3"
        _write_mp3(f)
        (tmp_path / "song.mp3").write_bytes(b"existing")
        result = _strip_untested_suffix(f, logging.getLogger("t"), "station")
        assert result is None
        assert f.exists()

    def test_rename_oserror_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        f = tmp_path / "song.untested.mp3"
        _write_mp3(f)

        def _fail_rename(self: Path, target: Path) -> Path:
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "rename", _fail_rename)
        result = _strip_untested_suffix(f, logging.getLogger("t"), "station")
        assert result is None


# ── correct_fingerprint_result ──


class TestCorrectFingerprintResult:
    def test_mb_none_returns_original(self):
        result = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        corrected = correct_fingerprint_result(result, None)
        assert corrected is result

    def test_mb_without_artist_returns_original(self):
        result = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        mb = MusicBrainzData(recording_id="r1", recording_artist=None)
        corrected = correct_fingerprint_result(result, mb)
        assert corrected is result

    def test_mb_overrides_artist_only(self):
        result = FingerprintResult(artist="AcoustIDArtist", title="AcoustIDTitle", score=0.9, recording_id="r1")
        mb = MusicBrainzData(recording_id="r1", recording_artist="MBArtist", recording_title=None)
        corrected = correct_fingerprint_result(result, mb)
        assert corrected.artist == "MBArtist"
        assert corrected.title == "AcoustIDTitle"
        assert corrected.score == 0.9
        assert corrected.recording_id == "r1"

    def test_mb_overrides_both(self):
        result = FingerprintResult(artist="A", title="T", score=0.95, recording_id="r1")
        mb = MusicBrainzData(recording_id="r1", recording_artist="MBArtist", recording_title="MBTitle")
        corrected = correct_fingerprint_result(result, mb)
        assert corrected.artist == "MBArtist"
        assert corrected.title == "MBTitle"
        assert corrected.score == 0.95
        assert corrected.recording_id == "r1"

    def test_itunes_fallback_when_mb_empty(self):
        result = FingerprintResult(artist="AcoustIDArtist", title="AcoustIDTitle", score=0.9, recording_id="r1")
        enriched = EnrichedInfo(artist="ITunesArtist", title="ITunesTitle")
        corrected = correct_fingerprint_result(result, None, enriched)
        assert corrected.artist == "ITunesArtist"
        assert corrected.title == "ITunesTitle"

    def test_itunes_fallback_ignored_when_same_as_acoustid(self):
        result = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        enriched = EnrichedInfo(artist="A", title="T")
        corrected = correct_fingerprint_result(result, None, enriched)
        assert corrected is result

    def test_mb_wins_over_itunes(self):
        result = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        mb = MusicBrainzData(recording_id="r1", recording_artist="MBArtist", recording_title="MBTitle")
        enriched = EnrichedInfo(artist="ITunesArtist", title="ITunesTitle")
        corrected = correct_fingerprint_result(result, mb, enriched)
        assert corrected.artist == "MBArtist"
        assert corrected.title == "MBTitle"


# ── _fetch_artist_image ──


class TestFetchArtistImage:
    async def test_returns_image_on_success(self):
        provider = AsyncMock()
        provider.fetch_artist_image.return_value = b"img"
        result = await _fetch_artist_image(provider, "Artist", "station", logging.getLogger("t"))
        assert result == b"img"

    async def test_returns_none_on_exception(self):
        provider = AsyncMock()
        provider.fetch_artist_image.side_effect = RuntimeError("boom")
        result = await _fetch_artist_image(provider, "Artist", "station", logging.getLogger("t"))
        assert result is None

    async def test_returns_none_when_provider_returns_none(self):
        provider = AsyncMock()
        provider.fetch_artist_image.return_value = None
        result = await _fetch_artist_image(provider, "Artist", "station", logging.getLogger("t"))
        assert result is None


# ── _fetch_cover_data ──


class TestFetchCoverData:
    async def test_all_success(self):
        cover_provider = AsyncMock()
        cover_provider.fetch_cover_by_recording_id.return_value = b"cover"
        cover_provider.fetch_recording_data.return_value = MusicBrainzData(recording_id="r1")
        pop = AsyncMock()
        pop.fetch_artist_image.return_value = b"art"
        cover, mb, art = await _fetch_cover_data(
            cover_provider,
            "r1",
            pop,
            "Artist",
            "station",
            logging.getLogger("t"),
        )
        assert cover == b"cover"
        assert mb is not None
        assert art == b"art"

    async def test_cover_fails_returns_none(self):
        cover_provider = AsyncMock()
        cover_provider.fetch_cover_by_recording_id.side_effect = RuntimeError("boom")
        cover_provider.fetch_recording_data.return_value = None
        cover, mb, art = await _fetch_cover_data(
            cover_provider,
            "r1",
            None,
            "",
            "station",
            logging.getLogger("t"),
        )
        assert cover is None
        assert mb is None
        assert art is None

    async def test_without_popularity_two_tasks(self):
        cover_provider = AsyncMock()
        cover_provider.fetch_cover_by_recording_id.return_value = b"cover"
        cover_provider.fetch_recording_data.return_value = None
        cover, mb, art = await _fetch_cover_data(
            cover_provider,
            "r1",
            None,
            "",
            "station",
            logging.getLogger("t"),
        )
        assert cover == b"cover"
        assert mb is None
        assert art is None

    async def test_all_fail(self):
        cover_provider = AsyncMock()
        cover_provider.fetch_cover_by_recording_id.side_effect = RuntimeError("x")
        cover_provider.fetch_recording_data.side_effect = RuntimeError("y")
        pop = AsyncMock()
        pop.fetch_artist_image.side_effect = RuntimeError("z")
        cover, mb, art = await _fetch_cover_data(
            cover_provider,
            "r1",
            pop,
            "Artist",
            "station",
            logging.getLogger("t"),
        )
        assert cover is None
        assert mb is None
        assert art is None


# ── _fetch_lyrics ──


class TestFetchLyrics:
    async def test_success(self):
        provider = AsyncMock()
        provider.fetch.return_value = "la la la"
        result = await _fetch_lyrics(provider, "A", "T", logging.getLogger("t"), "station")
        assert result == "la la la"

    async def test_exception_returns_none(self):
        provider = AsyncMock()
        provider.fetch.side_effect = RuntimeError("boom")
        result = await _fetch_lyrics(provider, "A", "T", logging.getLogger("t"), "station")
        assert result is None


# ── FileProcessor lifecycle ──


class TestFileProcessorLifecycle:
    async def test_start_creates_inbox_and_task(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        await proc.start()
        assert proc._task is not None
        assert proc._inbox.exists()
        await proc.stop()
        assert proc._task is None

    async def test_stop_cleans_up_task(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        await proc.start()
        assert proc._task is not None
        await proc.stop()
        assert proc._task is None


# ── _drain_inbox ──


class TestDrainInbox:
    async def test_empty_inbox_no_processing(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        await proc._drain_inbox()
        proc._fingerprint.fingerprint.assert_not_called()  # type: ignore[attr-defined]

    async def test_processes_all_mp3s(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        for name in ("a.mp3", "b.mp3", "c.mp3"):
            (proc._inbox / name).write_bytes(b"\xff\xfb\x00\x00")
        # _process_one will fail on rename because the files are real
        # but we mock _process_one to count calls
        call_count = 0

        async def _mock_process(mp3_path: Path) -> None:
            nonlocal call_count
            call_count += 1

        proc._process_one = _mock_process  # type: ignore[method-assign]
        await proc._drain_inbox()
        assert call_count == 3

    async def test_drain_batch_parallel(self, tmp_path: Path):
        """Neue Inbox-Dateien starten sofort parallel (kein 1s-Stagger)."""
        import time

        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        for name in ("a.mp3", "b.mp3", "c.mp3"):
            (proc._inbox / name).write_bytes(b"\xff\xfb\x00\x00")
        timestamps: list[float] = []

        async def _mock_process(mp3_path: Path) -> None:
            await asyncio.sleep(0.05)
            timestamps.append(time.monotonic())

        proc._process_one = _mock_process  # type: ignore[method-assign]
        await proc._drain_inbox()
        assert len(timestamps) == 3
        # Alle laufen parallel (Stagger weg) → Gesamt-Spread deutlich unter 1s.
        spread = max(timestamps) - min(timestamps)
        assert spread < 0.9, f"Dateien sollten parallel laufen, spread={spread:.2f}s"

    async def test_drain_respects_max_concurrent(self, tmp_path: Path):
        """max_concurrent begrenzt die gleichzeitig laufenden Dateien."""
        proc = _make_processor(tmp_path)
        proc._settings = proc._settings.model_copy(update={"max_concurrent": 2})
        proc._inbox.mkdir(parents=True, exist_ok=True)
        for name in ("a.mp3", "b.mp3", "c.mp3", "d.mp3"):
            (proc._inbox / name).write_bytes(b"\xff\xfb\x00\x00")

        active = 0
        peak = 0

        async def _mock_process(mp3_path: Path) -> None:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.1)
            active -= 1

        proc._process_one = _mock_process  # type: ignore[method-assign]
        await proc._drain_inbox()
        assert peak <= 2, f"max_concurrent=2 verletzt: peak={peak}"

    async def test_stop_event_aborts_drain(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        (proc._inbox / "a.mp3").write_bytes(b"\xff\xfb")
        proc._stop_event.set()
        await proc._drain_inbox()
        proc._fingerprint.fingerprint.assert_not_called()  # type: ignore[attr-defined]  # type: ignore[attr-defined]


# ── _process_one ──


class TestProcessOne:
    async def test_rename_succeeds_processes_file(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3)

        called = False

        async def _mock_file(proc_path: Path) -> None:
            nonlocal called
            called = True

        proc._process_file = _mock_file  # type: ignore[method-assign]
        await proc._process_one(mp3)
        assert called

    async def test_rename_fails_skips(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        mp3 = tmp_path / "nonexistent.mp3"
        await proc._process_one(mp3)
        proc._fingerprint.fingerprint.assert_not_called()  # type: ignore[attr-defined]  # type: ignore[attr-defined]


# ── _move_to_work_dir ──


class TestMoveToWorkDir:
    async def test_success(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        src = tmp_path / "song.processing"
        _write_mp3(src)
        result = await proc._move_to_work_dir(src)
        assert result is not None
        assert result.parent == proc._settings.work_dir
        assert result.exists()
        assert not src.exists()

    async def test_oserror_returns_none_and_cleans(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        src = tmp_path / "missing.processing"
        result = await proc._move_to_work_dir(src)
        assert result is None


# ── _fingerprint_and_validate ──


class TestFingerprintAndValidate:
    async def test_success_returns_result(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        expected = FingerprintResult(artist="A", title="T", score=0.95, recording_id="r1")
        proc._fingerprint.fingerprint.return_value = expected  # type: ignore[attr-defined]
        result = await proc._fingerprint_and_validate(work_path)
        assert result == expected
        assert work_path.exists()

    async def test_non_retriable_deletes_file(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        proc._fingerprint.fingerprint.side_effect = NonRetriableFingerprintError("corrupt")  # type: ignore[attr-defined]
        result = await proc._fingerprint_and_validate(work_path)
        assert result is None
        assert not work_path.exists()

    async def test_fingerprint_error_moves_to_temp(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        proc._fingerprint.fingerprint.side_effect = FingerprintError("infra error")  # type: ignore[attr-defined]
        result = await proc._fingerprint_and_validate(work_path)
        assert result is None
        # Datei sollte nach temp_dir verschoben worden sein
        assert not work_path.exists()

    async def test_empty_recording_id_is_accepted(self, tmp_path: Path):
        """Eine leere recording_id (z.B. usermeta-Treffer ohne MBID) ist KEIN
        Grund zum Löschen — artist/title + Score reichen aus."""
        proc = _make_processor(tmp_path)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        expected = FingerprintResult(
            artist="A",
            title="T",
            score=0.95,
            recording_id="",
        )
        proc._fingerprint.fingerprint.return_value = expected  # type: ignore[attr-defined]
        result = await proc._fingerprint_and_validate(work_path)
        assert result == expected
        assert work_path.exists()

    async def test_score_too_low_deletes_file(self, tmp_path: Path):
        proc = _make_processor(tmp_path, acoustid_min_score=0.99)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="A",
            title="T",
            score=0.5,
            recording_id="r1",
        )
        result = await proc._fingerprint_and_validate(work_path)
        assert result is None
        assert not work_path.exists()

    async def test_fingerprint_returns_none_deletes_file(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        proc._fingerprint.fingerprint.return_value = None  # type: ignore[attr-defined]
        result = await proc._fingerprint_and_validate(work_path)
        assert result is None
        assert not work_path.exists()


# ── _enrich_parallel ──


class TestEnrichParallel:
    async def test_all_success(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._metadata.fetch.return_value = EnrichedInfo(album="Album")  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._lyrics_provider = AsyncMock()
        proc._lyrics_provider.fetch.return_value = None
        result_fp = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        work_path = tmp_path / "song.mp3"
        enriched, _cover, _art, _lyrics = await proc._enrich_parallel(result_fp, track, work_path)
        assert enriched is not None
        assert enriched.album == "Album"

    async def test_itunes_exception_does_not_crash(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._metadata.fetch.side_effect = RuntimeError("iTunes down")  # type: ignore[attr-defined]
        result_fp = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        work_path = tmp_path / "song.mp3"
        enriched, cover, _art, _lyrics2 = await proc._enrich_parallel(result_fp, track, work_path)
        assert enriched is None
        assert cover is None


# ── _compute_destination_and_score ──


class TestComputeDestinationAndScore:
    async def test_no_existing_file(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        result_fp = FingerprintResult(artist="A", title="T", score=0.9, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        provenance, final_path, old_paths = await proc._compute_destination_and_score(
            result_fp,
            track,
            None,
            tmp_path / "work.mp3",
        )
        assert provenance == "tag/tag"
        assert final_path is not None
        assert final_path.parent == proc._settings.destination / "A"
        assert old_paths == []


# ── _finalize_and_move ──


class TestFinalizeAndMove:
    def _meta(self, artist: str, title: str, recording_id: str) -> CollectedMetadata:
        from radio_ripper.domain.models import EnrichedInfo as _EnrichedInfo

        result = FingerprintResult(artist=artist, title=title, score=0.9, recording_id=recording_id)
        track = TrackInfo(stream_title=f"{artist} - {title}", artist=artist, title=title)
        return CollectedMetadata(
            result=result,
            track=track,
            enriched=_EnrichedInfo(),
            mb_data=None,
            deezer_data=None,
            deezer_cover=None,
            cover_from_caa=None,
            cover_from_enrich=None,
            deezer_attempted=False,
            artist_image=None,
            lyrics=None,
        )

    async def test_moves_to_expected_path(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Wrong"
        dest.mkdir(parents=True)
        src = dest / "Wrong - Song.mp3"
        _write_mp3(src, size=512)
        meta = self._meta("Artist", "Title", "r1")
        await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)
        target = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert target.exists(), f"expected {target}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not src.exists()

    async def test_better_version_wins_at_target(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        # Bestehende Datei mit niedrigerem Score am Ziel
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        a = ID3()
        a.add(TPE1(encoding=3, text="Artist"))
        a.add(TIT2(encoding=3, text="Title"))
        a.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="r1"))
        a.add(TXXX(encoding=3, desc="AcoustID Score", text="0.5"))
        a.save(existing, v2_version=3)
        # Neue Datei (Score 0.9 via meta) an anderem Ort
        src_dir = tmp_path / "staging"
        src_dir.mkdir(parents=True)
        src = src_dir / "Artist - Title.mp3"
        _write_mp3(src, size=512)
        meta = self._meta("Artist", "Title", "r1")
        await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)
        assert existing.exists()  # bessere Version ersetzt am Ziel
        assert not src.exists()

    async def test_collision_keeps_existing(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        a = ID3()
        a.add(TPE1(encoding=3, text="Artist"))
        a.add(TIT2(encoding=3, text="Title"))
        a.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="other-rec"))
        a.save(existing, v2_version=3)
        src = tmp_path / "src.mp3"
        _write_mp3(src, size=512)
        meta = self._meta("Artist", "Title", "r1")
        ok = await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)
        # Kollision: existierende (andere recording_id) bleibt, die neue Datei wird
        # aufbewahrt (Deletion-Policy) — sie wandert nach manual_review/, nicht gelöscht.
        assert existing.exists()
        assert not src.exists()
        assert ok is False, "aufbewahrte Datei darf nicht als Erfolg gemeldet werden"
        assert list(proc._manual_review_dir.glob("*.mp3")) == [proc._manual_review_dir / "src.mp3"], (
            "Kollisionsdatei muss in manual_review/ aufbewahrt werden"
        )

    async def test_collision_keeps_library_file_when_delete_source_false(self, tmp_path: Path):
        """Bug 1: Enrich-Kollision darf die Bibliotheksdatei NIE löschen.

        source_path ist hier die echte Datei in destination/ (Enrich-Flow),
        staged_path die getaggte Staging-Kopie. Bei Kollision wird nur die
        Staging-Kopie verworfen — das Original bleibt unangetastet.
        """
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        a = ID3()
        a.add(TPE1(encoding=3, text="Artist"))
        a.add(TIT2(encoding=3, text="Title"))
        a.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="other-rec"))
        a.save(existing, v2_version=3)

        original = proc._settings.destination / "Old" / "Original.mp3"
        original.parent.mkdir(parents=True)
        _write_mp3(original, size=512)

        stage_dir = tmp_path / "staging"
        stage_dir.mkdir(parents=True)
        stage = stage_dir / "Original.mp3"
        _write_mp3(stage, size=512)

        meta = self._meta("Artist", "Title", "r1")
        ok = await proc._finalize_and_move(
            meta=meta,
            source_path=original,
            staged_path=stage,
            delete_source=False,
        )
        assert ok is False
        assert original.exists(), "Bibliotheksdatei wurde fälschlich gelöscht"
        assert existing.exists()
        assert not stage.exists(), "Staging-Kopie wird verworfen"

    async def test_old_copies_deleted_after_successful_move(self, tmp_path: Path):
        """Bug 2: Duplikat-Kopien werden erst nach erfolgreichem Move gelöscht."""
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination
        src = dest / "Old" / "src.mp3"
        src.parent.mkdir(parents=True)
        _write_mp3(src, size=512)
        old_a = dest / "A" / "a.mp3"
        old_a.parent.mkdir(parents=True)
        _write_mp3(old_a, size=512)
        old_b = dest / "B" / "b.mp3"
        old_b.parent.mkdir(parents=True)
        _write_mp3(old_b, size=512)

        meta = self._meta("Artist", "Title", "r1")
        ok = await proc._finalize_and_move(
            meta=meta,
            source_path=src,
            staged_path=src,
            old_paths=[old_a, old_b],
        )
        target = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert ok is True
        assert target.exists()
        assert not old_a.exists(), "alte Kopie wurde nicht entfernt"
        assert not old_b.exists(), "alte Kopie wurde nicht entfernt"

    async def test_old_copies_kept_when_file_rejected(self, tmp_path: Path):
        """Bug 2: Bei Kollision/Verwerfen werden Duplikat-Kopien NICHT gelöscht."""
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        a = ID3()
        a.add(TPE1(encoding=3, text="Artist"))
        a.add(TIT2(encoding=3, text="Title"))
        a.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="other-rec"))
        a.save(existing, v2_version=3)
        src = tmp_path / "src.mp3"
        _write_mp3(src, size=512)
        old_copy = proc._settings.destination / "Other" / "copy.mp3"
        old_copy.parent.mkdir(parents=True)
        _write_mp3(old_copy, size=512)

        meta = self._meta("Artist", "Title", "r1")
        ok = await proc._finalize_and_move(
            meta=meta,
            source_path=src,
            staged_path=src,
            old_paths=[old_copy],
        )
        assert ok is False
        assert old_copy.exists(), "Duplikat wurde trotz Verwerfen der neuen Datei gelöscht"

    async def test_returns_true_when_moved(self, tmp_path: Path):
        """Bug 3-Grundlage: erfolgreicher Move wird als Erfolg gemeldet (→ Eviction erlaubt)."""
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Old"
        dest.mkdir(parents=True)
        src = dest / "song.mp3"
        _write_mp3(src, size=512)
        meta = self._meta("Artist", "Title", "r1")
        ok = await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)
        assert ok is True

    def _write_existing(
        self, path: Path, *, recording_id: str, score: str | None = None, isrc: str | None = None
    ) -> None:
        """Bestehende Ziel-Datei mit recording_id/score/isrc vorbereiten."""
        from mutagen.id3 import ID3, TIT2, TPE1, TSRC, TXXX

        a = ID3()
        a.add(TPE1(encoding=3, text="Artist"))
        a.add(TIT2(encoding=3, text="Title"))
        a.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text=recording_id))
        if score is not None:
            a.add(TXXX(encoding=3, desc="AcoustID Score", text=score))
        if isrc is not None:
            a.add(TSRC(encoding=3, text=isrc))
        a.save(path, v2_version=3)

    def _meta_with_isrc(self, recording_id: str, isrc: str, score: float) -> CollectedMetadata:
        """Meta mit Deezer-ISRC (Quelle für den ISRC-Fallback)."""
        from radio_ripper.domain.models import EnrichedInfo as _EnrichedInfo

        result = FingerprintResult(artist="Artist", title="Title", score=score, recording_id=recording_id)
        track = TrackInfo(stream_title="Artist - Title", artist="Artist", title="Title")
        return CollectedMetadata(
            result=result,
            track=track,
            enriched=_EnrichedInfo(),
            mb_data=None,
            deezer_data=DeezerData(isrc=isrc),
            deezer_cover=None,
            cover_from_caa=None,
            cover_from_enrich=None,
            deezer_attempted=True,
            artist_image=None,
            lyrics=None,
        )

    async def test_isrc_fallback_new_version_wins(self, tmp_path: Path):
        """Option 1: verschiedene recording_ids + gleicher ISRC = gleicher Song → bessere Version ersetzt."""
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        self._write_existing(existing, recording_id="old-rec", score="0.5", isrc="ISRC1")

        # staged = bereits getaggte neue Version (wie Phase 8 in der Pipeline)
        src = tmp_path / "src.mp3"
        _write_mp3(src, size=512)
        self._write_existing(src, recording_id="new-rec", score="0.9", isrc="ISRC1")
        meta = self._meta_with_isrc("new-rec", "ISRC1", 0.9)
        ok = await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)

        assert ok is True
        assert not src.exists()
        # Bestehende Datei wurde ersetzt (neue, bessere Version)
        tags = read_tags_from_file(existing)
        assert tags["recording_id"] == "new-rec"

    async def test_isrc_fallback_existing_wins(self, tmp_path: Path):
        """Option 1: ISRC-Match, aber bestehende Version ist besser → neue wird verworfen."""
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        self._write_existing(existing, recording_id="old-rec", score="0.95", isrc="ISRC1")

        src = tmp_path / "src.mp3"
        _write_mp3(src, size=512)
        self._write_existing(src, recording_id="new-rec", score="0.5", isrc="ISRC1")
        meta = self._meta_with_isrc("new-rec", "ISRC1", 0.5)
        ok = await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)

        assert ok is False
        assert not src.exists()
        tags = read_tags_from_file(existing)
        assert tags["recording_id"] == "old-rec", "bessere bestehende Version bleibt"

    async def test_no_isrc_match_stays_collision(self, tmp_path: Path):
        """Verschiedene recording_ids UND verschiedene ISRCs → weiterhin Kollision."""
        proc = _make_processor(tmp_path)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        _write_mp3(existing, size=512)
        self._write_existing(existing, recording_id="old-rec", score="0.5", isrc="OTHER")

        src = tmp_path / "src.mp3"
        _write_mp3(src, size=512)
        self._write_existing(src, recording_id="new-rec", score="0.9", isrc="ISRC1")
        meta = self._meta_with_isrc("new-rec", "ISRC1", 0.9)
        ok = await proc._finalize_and_move(meta=meta, source_path=src, staged_path=src)

        assert ok is False
        assert existing.exists()
        tags = read_tags_from_file(existing)
        assert tags["recording_id"] == "old-rec", "anderer Song bleibt unangetastet"


# ── _process_file (End-to-End Happy Path) ──


class TestProcessFileHappyPath:
    async def test_full_pipeline_delivers_to_destination(self, tmp_path: Path):
        proc = _make_processor(tmp_path, min_popularity_rank=0)
        # Inbox-Datei anlegen
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        # Fingerprint erfolgreich
        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        # Metadata: None (nutzt track-title als Album-Fallback)
        proc._metadata.fetch.return_value = None  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        # Cover-Provider: None (kein Cover, keine MB-Korrektur)
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc._process_one(mp3)

        # Datei sollte in destination/Artist/Artist - Title.mp3 landen
        expected = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"


class TestEnrichExistingFiles:
    async def test_enriches_missing_album_only(self, tmp_path: Path):
        """Bestandsdatei mit fehlendem Album wird über denselben Flow angereichert."""
        from mutagen.id3 import ID3, TIT2, TPE1

        from radio_ripper.services.tagging import ID3Tagger

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        proc._tagger = ID3Tagger()
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.mp3"
        _write_mp3(f, size=512)
        # Datei mit Artist/Title aber OHNE Album vorbereiten
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.save(f, v2_version=3)

        # iTunes liefert Album + Genre
        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            album="Great Album",
            genre="Rock",
        )
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._fingerprint.fingerprint = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc.enrich_existing_files()

        # Neue Benennung: Album-Unterordner (konsistent zu neuen MP3s).
        expected = proc._settings.destination / "Artist" / "Great Album" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        audio2 = ID3(expected)
        assert (alb := audio2.get("TALB")) is not None and alb.text == ["Great Album"]
        assert (g := audio2.get("TCON")) is not None and g.text == ["Rock"]

    async def test_skips_complete_files(self, tmp_path: Path):
        """Datei mit Album + Genre + Cover wird NICHT angefasst."""
        from mutagen.id3 import APIC, ID3, TALB, TCON, TIT2, TPE1

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.add(TALB(encoding=3, text="Album"))
        audio.add(TCON(encoding=3, text="Rock"))
        audio.add(APIC(encoding=3, mime="image/jpeg", type=3, desc="Cover", data=b"\xff\xd8\xff" + b"\x00" * 64))
        audio.save(f, v2_version=3)
        before = f.read_bytes()

        await proc.enrich_existing_files()

        # Datei unverändert
        assert f.read_bytes() == before
        proc._metadata.fetch.assert_not_called()  # type: ignore[attr-defined]

    async def test_never_deletes_existing_file(self, tmp_path: Path):
        """Bestandsdateien werden bei der Anreicherung nie gelöscht (auch ohne recording_id)."""
        from mutagen.id3 import ID3, TIT2, TPE1

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.save(f, v2_version=3)

        # alle Provider liefern nichts
        proc._metadata.fetch.return_value = None  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._fingerprint.fingerprint = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc.enrich_existing_files()

        assert f.exists()
        # kein Rest im work_dir (Staging-Kopie aufgeräumt)
        assert not list((proc._settings.work_dir).rglob("enrich-*"))

    async def test_enrich_keeps_original_when_replace_fails(self, tmp_path: Path):
        """Ersetzen schlägt fehl → Original bleibt unverändert, Staging aufgeräumt."""
        from unittest.mock import patch

        from mutagen.id3 import ID3, TIT2, TPE1

        from radio_ripper.services.tagging import ID3Tagger

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        proc._tagger = ID3Tagger()
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.save(f, v2_version=3)
        before = f.read_bytes()

        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            album="Great Album",
            genre="Rock",
        )
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._fingerprint.fingerprint = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        with patch(
            "radio_ripper.services.processor.shutil.move",
            side_effect=OSError("Invalid cross-device link"),
        ):
            await proc.enrich_existing_files()

        # Original unverändert
        assert f.read_bytes() == before
        # Staging aufgeräumt
        assert not list((proc._settings.work_dir).rglob("enrich-*"))

    async def test_caa_cover_wins_over_itunes(self, tmp_path: Path):
        """Cover-Priorität: CAA (verifiziert via recording_id) gewinnt vor iTunes."""
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        from radio_ripper.services.tagging import ID3Tagger

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        proc._tagger = ID3Tagger()
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="rec-12345"))
        audio.save(f, v2_version=3)

        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist", title="Title", artwork_url="http://itunes.example/cover.jpg"
        )
        proc._metadata.download_image = AsyncMock(return_value=b"ITUNES-COVER-DATA")  # type: ignore[method-assign]
        # CAA liefert ein anderes Cover (soll gewinnen)
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = b"CAA-COVER-DATA"
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc.enrich_existing_files()

        target = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert target.exists()
        t = ID3(target)
        apic = t.get("APIC:Cover")
        assert apic is not None
        assert apic.data == b"CAA-COVER-DATA"


class TestNormalizeFilenames:
    async def test_renames_file_with_wrong_name(self, tmp_path: Path):
        """Datei mit falschem Namen wird zum Tag-Standard (Artist - Title) umbenannt."""
        from mutagen.id3 import ID3, TIT2, TPE1

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        dest = proc._settings.destination / "FalscherName"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "FalscherName - FalscherTitel.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.save(f, v2_version=3)

        await proc.normalize_filenames()

        target = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert target.exists(), f"expected {target}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not f.exists()

    async def test_renames_duplicate_suffix_file(self, tmp_path: Path):
        """Datei mit .1-Suffix (gleiche recording_id) wird zum Standard umbenannt."""
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.1.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="rec-1"))
        audio.save(f, v2_version=3)

        await proc.normalize_filenames()

        target = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert target.exists(), f"expected {target}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not f.exists()

    async def test_keeps_correct_name(self, tmp_path: Path):
        """Korrekt benannte Datei bleibt unverändert."""
        from mutagen.id3 import ID3, TIT2, TPE1

        proc = _make_processor(tmp_path, min_popularity_rank=0)
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True, exist_ok=True)
        f = dest / "Artist - Title.mp3"
        _write_mp3(f, size=512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.save(f, v2_version=3)

        await proc.normalize_filenames()

        assert f.exists()
        # keine zusätzlichen mp3
        assert len(list(proc._settings.destination.rglob("*.mp3"))) == 1


class TestProcessFileFailurePaths:
    async def test_fingerprint_failure_cleans_up(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)
        proc._fingerprint.fingerprint.side_effect = NonRetriableFingerprintError("corrupt")  # type: ignore[attr-defined]

        await proc._process_one(mp3)

        # Datei sollte gelöscht sein
        assert not mp3.exists()
        # und nicht in destination
        assert not proc._settings.destination.exists() or not list(proc._settings.destination.rglob("*.mp3"))

    async def test_tag_write_failure_moves_to_temp_not_destination(self, tmp_path: Path):
        proc = _make_processor(tmp_path, min_popularity_rank=0)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = None  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None
        proc._tagger.write_all.side_effect = RuntimeError("tag write boom")  # type: ignore[attr-defined]

        await proc._process_one(mp3)

        # ungetaggte Datei darf NICHT in destination landen (sonst "Unknown Album"/kein Cover in Navidrome)
        assert not proc._settings.destination.exists() or not list(proc._settings.destination.rglob("*.mp3"))
        # Datei sollte nach temp_dir (failed) verschoben worden sein
        moved = list(proc._temp_dir.rglob("*.mp3"))
        assert len(moved) == 1
        assert moved[0].exists()

    async def test_tag_write_retries_then_succeeds(self, tmp_path: Path):
        proc = _make_processor(tmp_path, min_popularity_rank=0)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = None  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        calls = 0

        def _flaky(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient tag write boom")

        proc._tagger.write_all.side_effect = _flaky  # type: ignore[attr-defined]

        await proc._process_one(mp3)

        # zweiter Versuch war erfolgreich → Datei landet in destination
        expected = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert calls == 2
        assert not list(proc._temp_dir.rglob("*.mp3"))

    async def test_deezer_refetch_network_error_does_not_delete(self, tmp_path: Path):
        """Netzwerkfehler beim Deezer-Re-Fetch (nach MB-Korrektur) darf die Datei NICHT löschen.

        Vor dem Fix blieb deezer_attempted=True + deezer_data=None → die Popularitäts-Prüfung
        behandelte die Datei als "nicht auf Deezer" und löschte sie fälschlicherweise.
        """
        proc = _make_processor(tmp_path, min_popularity_rank=100)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = None  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        # Cover-Provider: kein Cover, aber MB liefert Künstler/Titel-Korrektur
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = MusicBrainzData(
            recording_id="rec-1",
            recording_artist="MBCorrectedArtist",
            recording_title="MBCorrectedTitle",
        )

        # Deezer: erster Fetch liefert Daten, Re-Fetch nach MB-Korrektur wirft Netzwerkfehler
        deezer = AsyncMock()
        deezer.fetch.side_effect = [DeezerData(rank=1000), RuntimeError("network down")]
        proc._deezer_provider = deezer

        await proc._process_one(mp3)

        # Datei darf NICHT gelöscht sein → landet verarbeitet in destination
        expected = proc._settings.destination / "MBCorrectedArtist" / "MBCorrectedArtist - MBCorrectedTitle.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not list(proc._temp_dir.rglob("*.mp3"))

    async def test_deezer_primary_network_error_does_not_delete(self, tmp_path: Path):
        """Netzwerkfehler beim primären Deezer-Fetch darf die Datei NICHT löschen.

        DeezerMetadataProvider.fetch() re-raised Netzwerkfehler → der Processor
        setzt deezer_attempted=False → die Popularitäts-Prüfung überspringt und
        löscht die Datei nicht als "nicht auf Deezer".
        """
        proc = _make_processor(tmp_path, min_popularity_rank=100)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = None  # type: ignore[attr-defined]
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        # Deezer-API down → fetch wirft (kein None!)
        deezer = AsyncMock()
        deezer.fetch.side_effect = RuntimeError("network down")
        proc._deezer_provider = deezer

        await proc._process_one(mp3)

        # Datei darf NICHT gelöscht sein → landet verarbeitet in destination
        expected = proc._settings.destination / "Artist" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not list(proc._temp_dir.rglob("*.mp3"))


class TestEarlyPopularityCheck:
    """Stufe B — Deezer-Vorab-Call ist Advisory, löscht aber NIE (Deletion-Policy)."""

    async def test_unpopular_track_is_kept_despite_low_rank(self, tmp_path: Path):
        """rank < min → Datei wird NICHT gelöscht, läuft durch die Pipeline ins Ziel."""
        proc = _make_processor(tmp_path, min_popularity_rank=100)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist", title="Title", album="Album", genre="Rock"
        )
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None
        deezer = AsyncMock()
        deezer.fetch.return_value = DeezerData(rank=50)  # < min → aufbewahrt
        proc._deezer_provider = deezer

        await proc._process_one(mp3)

        expected = proc._settings.destination / "Artist" / "Album" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not mp3.exists()
        deezer.fetch.assert_awaited_once()
        assert not list(proc._manual_review_dir.rglob("*.mp3")), "aufbewahrt? Nein — landet normal im Ziel"

    async def test_unknown_track_not_on_deezer_is_kept(self, tmp_path: Path):
        """Deezer: 0 Treffer → 'nicht auf Deezer' → NICHT löschen, Pipeline läuft weiter."""
        proc = _make_processor(tmp_path, min_popularity_rank=100)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist", title="Title", album="Album", genre="Rock"
        )
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None
        deezer = AsyncMock()
        deezer.fetch.return_value = None  # 0 Deezer-Treffer
        proc._deezer_provider = deezer

        await proc._process_one(mp3)

        expected = proc._settings.destination / "Artist" / "Album" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        assert not mp3.exists()
        deezer.fetch.assert_awaited_once()

    async def test_popular_reaches_full_pipeline(self, tmp_path: Path):
        """rank >= min → Datei überlebt Stufe B, der Deezer-Call wird NICHT doppelt gemacht."""
        proc = _make_processor(tmp_path, min_popularity_rank=100)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="Artist",
            title="Title",
            score=0.95,
            recording_id="rec-1",
        )
        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist", title="Title", album="Album", genre="Rock"
        )
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None
        deezer = AsyncMock()
        deezer.fetch.return_value = DeezerData(rank=1000, isrc="ISRC1")
        proc._deezer_provider = deezer

        await proc._process_one(mp3)

        expected = proc._settings.destination / "Artist" / "Album" / "Artist - Title.mp3"
        assert expected.exists(), f"expected {expected}, found: {list(proc._settings.destination.rglob('*.mp3'))}"
        # Stufe B + _collect_metadata: Deezer-Call genau EINMAL (kein Doppel-Fetch)
        deezer.fetch.assert_awaited_once()


class TestDeletionPolicy:
    """Deletion-Policy: unerwartete Fehler → manual_review/, NIE löschen."""

    async def test_unexpected_error_moves_to_manual_review(self, tmp_path: Path):
        """Beliebiger unerwarteter Fehler nach dem Move ins work_dir → aufbewahren."""
        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        mp3 = proc._inbox / "song.mp3"
        _write_mp3(mp3, size=512)

        proc._fingerprint.fingerprint.side_effect = RuntimeError("boom")  # type: ignore[attr-defined]

        await proc._process_one(mp3)

        assert not mp3.exists()
        assert list(proc._manual_review_dir.glob("*.mp3")) != [], "Datei muss aufbewahrt werden"
        assert not list(proc._settings.destination.rglob("*.mp3"))

    async def test_move_to_work_dir_failure_preserves_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Verschieben ins work_dir fehlgeschlagen → Datei wird aufbewahrt, nicht gelöscht."""
        import radio_ripper.services.processor as processor_mod

        proc = _make_processor(tmp_path)
        proc._inbox.mkdir(parents=True, exist_ok=True)
        src = proc._inbox / "song.mp3.processing"
        _write_mp3(src, size=512)

        real_move = processor_mod.shutil.move
        state = {"n": 0}

        def _fail_first_move(src_path: str, dst_path: str) -> Any:
            state["n"] += 1
            if state["n"] == 1:
                raise OSError("disk full")
            return real_move(src_path, dst_path)

        monkeypatch.setattr(processor_mod.shutil, "move", _fail_first_move)

        result = await proc._move_to_work_dir(src)

        assert result is None
        assert not src.exists()
        assert list(proc._manual_review_dir.glob("*.mp3")) == [proc._manual_review_dir / "song.mp3"]
