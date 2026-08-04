"""Tests for radio_ripper.services.processor."""

from __future__ import annotations

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
from radio_ripper.infra.config import Settings
from radio_ripper.services.fingerprint import (
    FingerprintError,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata_deezer import DeezerData
from radio_ripper.services.processor import (
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

    async def test_no_recording_id_deletes_file(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        work_path = tmp_path / "song.mp3"
        _write_mp3(work_path)
        proc._fingerprint.fingerprint.return_value = FingerprintResult(  # type: ignore[attr-defined]
            artist="A",
            title="T",
            score=0.95,
            recording_id="",
        )
        result = await proc._fingerprint_and_validate(work_path)
        assert result is None
        assert not work_path.exists()

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
        provenance, final_path, delete_old = await proc._compute_destination_and_score(
            result_fp,
            track,
            None,
            tmp_path / "work.mp3",
        )
        assert provenance == "tag/tag"
        assert final_path is not None
        assert final_path.parent == proc._settings.destination / "A"
        assert delete_old is None

    async def test_existing_better_score_returns_empty(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        result_fp = FingerprintResult(artist="A", title="T", score=0.5, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        # Bestehende Datei mit Score 0.9 erstellen
        dest_dir = proc._settings.destination / "A"
        dest_dir.mkdir(parents=True)
        existing = dest_dir / "A - T.mp3"
        existing.write_bytes(b"\xff\xfb\x00\x00")
        # ID3Tag mit Score 0.9 anlegen — wir mocken read_acoustid_score
        import radio_ripper.services.processor as proc_mod

        proc_mod.read_acoustid_score = lambda path: 0.9
        provenance, final_path, delete_old = await proc._compute_destination_and_score(
            result_fp,
            track,
            None,
            tmp_path / "work.mp3",
        )
        assert provenance == ""
        assert final_path is None
        assert delete_old is None

    async def test_existing_worse_score_returns_delete_old(self, tmp_path: Path):
        proc = _make_processor(tmp_path)
        result_fp = FingerprintResult(artist="A", title="T", score=0.95, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        dest_dir = proc._settings.destination / "A"
        dest_dir.mkdir(parents=True)
        existing = dest_dir / "A - T.mp3"
        existing.write_bytes(b"\xff\xfb\x00\x00")
        import radio_ripper.services.processor as proc_mod

        proc_mod.read_acoustid_score = lambda path: 0.5
        provenance, final_path, delete_old = await proc._compute_destination_and_score(
            result_fp,
            track,
            None,
            tmp_path / "work.mp3",
        )
        assert provenance == "tag/tag"
        assert final_path is not None
        assert delete_old == final_path


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
        proc._metadata.fetch_artist_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc.enrich_existing_files()

        audio2 = ID3(f)
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
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc.enrich_existing_files()

        assert f.exists()
        # kein Rest im work_dir (Staging-Kopie aufgeräumt)
        assert not list((proc._settings.work_dir).rglob("enrich-*"))


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
