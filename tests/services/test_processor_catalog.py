"""Tests für die Processor-Integration des Catalog:
- Catalog-basierten Versionsvergleich
- Live-Ausschluss
- Eviction (Simulation)
- Katalog-Upsert nach Pipeline-Abschluss
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from radio_ripper.domain.models import (
    EnrichedInfo,
    FingerprintResult,
    TrackInfo,
)
from radio_ripper.infra.catalog import SongRecord, SqliteCatalog
from radio_ripper.infra.config import Settings
from radio_ripper.services.metadata_deezer import DeezerData
from radio_ripper.services.processor import FileProcessor


def _settings(tmp_path: Path, **overrides: Any) -> Settings:
    defaults: dict[str, Any] = dict(
        destination=tmp_path / "out",
        work_dir=tmp_path / "work",
        source=tmp_path / "inbox",
        min_popularity_rank=0,
    )
    defaults.update(overrides)
    return Settings.model_validate(defaults)


def _make_processor(tmp_path: Path, catalog: SqliteCatalog | None = None, **overrides: Any) -> FileProcessor:
    settings = _settings(tmp_path, **overrides)
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
        catalog=catalog,
        logger=logging.getLogger("test-catalog"),
    )


@pytest.fixture
async def catalog(tmp_path: Path) -> AsyncGenerator[SqliteCatalog]:
    cat = SqliteCatalog(tmp_path / "catalog.db")
    yield cat
    await cat.aclose()


class TestCatalogVersionReplace:
    """Phase 4 — Catalog-basierter Versionsvergleich (cross-path, same MBID+ISRC)."""

    async def test_catalog_replace_better_version(self, tmp_path: Path, catalog: SqliteCatalog):
        # Alte Version unter einem *anderen* Pfad (anderes Album)
        old_path = tmp_path / "out" / "A" / "OldAlbum" / "A - T.mp3"
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"\xff\xfb\x00\x00")
        await catalog.upsert(
            SongRecord(
                file_path=str(old_path),
                recording_id="r1",
                isrc="ISRC1",
                artist="A",
                title="T",
                acoustid_score=0.85,
                bitrate=192,
                sample_rate=44100,
                popularity_rank=50000,
            )
        )
        proc = _make_processor(tmp_path, catalog=catalog)
        result = FingerprintResult(artist="A", title="T", score=0.95, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        _, final_path, old_paths = await proc._compute_destination_and_score(
            result,
            track,
            None,
            tmp_path / "work.mp3",
        )
        # Alte Kopie wird NICHT sofort gelöscht — sie wird als old_path gemeldet,
        # damit _finalize_and_move sie erst nach erfolgreichem Move entfernt.
        assert old_paths == [old_path]
        assert final_path is not None
        assert old_path.exists(), "Duplikat darf nicht vorzeitig gelöscht werden"

    async def test_catalog_skip_when_existing_better(self, tmp_path: Path, catalog: SqliteCatalog):
        # Alte Version unter anderem Pfad, höherer Score als neu
        old_path = tmp_path / "out" / "A" / "OldAlbum" / "A - T.mp3"
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"\xff\xfb\x00")
        await catalog.upsert(
            SongRecord(
                file_path=str(old_path),
                recording_id="r1",
                isrc="ISRC1",
                artist="A",
                title="T",
                acoustid_score=0.95,
                bitrate=320,
                sample_rate=44100,
                popularity_rank=50000,
            )
        )
        proc = _make_processor(tmp_path, catalog=catalog)
        # Neue Version: schlechterer Score (gleiche MBID + ISRC → same version)
        result = FingerprintResult(artist="A", title="T", score=0.80, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        provenance, final_path, _ = await proc._compute_destination_and_score(
            result,
            track,
            None,
            tmp_path / "work.mp3",
        )
        # Katalog entscheidet: bestehende ist besser → neue verwerfen
        assert provenance == "" and final_path is None

    async def test_catalog_skip_when_no_isrc_new_worse_score(self, tmp_path: Path, catalog: SqliteCatalog):
        # Alte Version ohne ISRC unter anderem Pfad, höherer Score → neue wird verworfen
        old_path = tmp_path / "out" / "A" / "OldAlbum" / "A - T.mp3"
        old_path.parent.mkdir(parents=True)
        old_path.write_bytes(b"\xff\xfb\x00")
        await catalog.upsert(
            SongRecord(
                file_path=str(old_path),
                recording_id="r1",
                isrc=None,
                artist="A",
                title="T",
                acoustid_score=0.95,
                bitrate=320,
                sample_rate=44100,
            )
        )
        proc = _make_processor(tmp_path, catalog=catalog)
        result = FingerprintResult(artist="A", title="T", score=0.80, recording_id="r1")
        track = TrackInfo(stream_title="A - T", artist="A", title="T")
        # Neue Version ist schlechter → wird verworfen (gleiche recording_id genügt für Vergleich)
        provenance, final_path, _ = await proc._compute_destination_and_score(
            result,
            track,
            None,
            tmp_path / "work.mp3",
        )
        assert provenance == "" and final_path is None


class TestEviction:
    """Phase 10 — Eviction bei vollem Sammlungslimit."""

    async def test_no_evict_under_limit(self, tmp_path: Path, catalog: SqliteCatalog):
        proc = _make_processor(
            tmp_path,
            catalog=catalog,
            max_collection_size=100,
            enable_eviction=True,
        )
        # 5 Songs im Katalog
        for i in range(5):
            await catalog.upsert(
                SongRecord(
                    file_path=f"/out/{i}.mp3",
                    artist=f"A{i}",
                    title=f"T{i}",
                    popularity_rank=i * 100 + 100,
                )
            )
        with patch("radio_ripper.services.processor.safe_unlink") as mock_unlink:
            await proc._maybe_evict(new_rank=100, final_path=tmp_path / "x.mp3")
            mock_unlink.assert_not_called()

    async def test_evict_when_over_limit(self, tmp_path: Path, catalog: SqliteCatalog):
        proc = _make_processor(
            tmp_path,
            catalog=catalog,
            max_collection_size=3,
            enable_eviction=True,
        )
        # 3 Songs, Ranks 100, 500, 999
        songs = [
            SongRecord(file_path="/out/a.mp3", artist="A", title="T", popularity_rank=100),
            SongRecord(file_path="/out/b.mp3", artist="B", title="T", popularity_rank=500),
            SongRecord(file_path="/out/c.mp3", artist="C", title="T", popularity_rank=999),
        ]
        for s in songs:
            await catalog.upsert(s)
        # Dateien anlegen
        for s in songs:
            p = tmp_path / "out" / Path(s.file_path).name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xfb\x00")
        # Patch: Dateisystem-Zugriff auf tmp_path
        proc._settings = proc._settings.model_copy(update={"destination": tmp_path / "out"})
        final_path = tmp_path / "new.mp3"

        with (
            patch("radio_ripper.services.processor.safe_unlink") as mock_unlink,
            patch.object(catalog, "find_least_popular", new=AsyncMock(return_value=songs)),
            patch.object(catalog, "remove", new=AsyncMock()),
        ):
            await proc._maybe_evict(new_rank=1000, final_path=final_path)
        # sollte jemand mit niedrigstem Rank (100) evicted haben
        mock_unlink.assert_called_once()
        evicted = mock_unlink.call_args[0][0]
        assert "a.mp3" in str(evicted) or Path(evicted).name == "a.mp3"

    async def test_evict_disabled_when_eviction_false(self, tmp_path: Path, catalog: SqliteCatalog):
        proc = _make_processor(
            tmp_path,
            catalog=catalog,
            max_collection_size=2,
            enable_eviction=False,  # deaktiviert
        )
        for i in range(3):
            await catalog.upsert(
                SongRecord(
                    file_path=f"/out/{i}.mp3",
                    artist=f"A{i}",
                    title=f"T{i}",
                    popularity_rank=100 + i,
                )
            )
        with patch("radio_ripper.services.processor.safe_unlink") as mock_unlink:
            await proc._maybe_evict(new_rank=1000, final_path=tmp_path / "x.mp3")
            mock_unlink.assert_not_called()

    async def test_evict_disabled_when_max_size_zero(self, tmp_path: Path, catalog: SqliteCatalog):
        proc = _make_processor(
            tmp_path,
            catalog=catalog,
            max_collection_size=0,
            enable_eviction=True,
        )
        with patch("radio_ripper.services.processor.safe_unlink") as mock_unlink:
            await proc._maybe_evict(new_rank=1000, final_path=tmp_path / "x.mp3")
            mock_unlink.assert_not_called()

    async def test_evict_fallback_when_no_rank_qualifies(self, tmp_path: Path, catalog: SqliteCatalog):
        """Wenn kein Song weniger populär als der neue ist, wird der absolut
        unpopulärste verdrängt — Sammlungslimit bleibt gewahrt."""
        proc = _make_processor(
            tmp_path,
            catalog=catalog,
            max_collection_size=2,
            enable_eviction=True,
        )
        for i in range(2):
            await catalog.upsert(
                SongRecord(
                    file_path=f"/out/{i}.mp3",
                    artist=f"A{i}",
                    title=f"T{i}",
                    popularity_rank=1000 + i,
                )
            )
            p = tmp_path / "out" / f"{i}.mp3"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"\xff\xfb\x00")
        proc._settings = proc._settings.model_copy(update={"destination": tmp_path / "out"})
        final_path = tmp_path / "new.mp3"

        with patch("radio_ripper.services.processor.safe_unlink") as mock_unlink:
            await proc._maybe_evict(new_rank=500, final_path=final_path)
        mock_unlink.assert_called_once()
        evicted = mock_unlink.call_args[0][0]
        assert "0.mp3" in str(evicted)

    async def test_evict_fallback_when_new_rank_none(self, tmp_path: Path, catalog: SqliteCatalog):
        """Auch ohne Deezer-Rank wird der unpopulärste Song verdrängt."""
        proc = _make_processor(
            tmp_path,
            catalog=catalog,
            max_collection_size=2,
            enable_eviction=True,
        )
        await catalog.upsert(
            SongRecord(
                file_path="/out/low.mp3",
                artist="A",
                title="T",
                popularity_rank=100,
            )
        )
        (tmp_path / "out" / "low.mp3").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "out" / "low.mp3").write_bytes(b"\xff\xfb\x00")
        await catalog.upsert(
            SongRecord(
                file_path="/out/high.mp3",
                artist="B",
                title="T",
                popularity_rank=999,
            )
        )
        (tmp_path / "out" / "high.mp3").write_bytes(b"\xff\xfb\x00")
        proc._settings = proc._settings.model_copy(update={"destination": tmp_path / "out"})
        final_path = tmp_path / "new.mp3"

        with patch("radio_ripper.services.processor.safe_unlink") as mock_unlink:
            await proc._maybe_evict(new_rank=None, final_path=final_path)
        mock_unlink.assert_called_once()
        evicted = mock_unlink.call_args[0][0]
        assert "low.mp3" in str(evicted)  # absolut unpopulärster


class TestCatalogUpsert:
    """Phase 9 — Katalog-Upsert nach erfolgreichem Move."""

    async def test_catalog_upsert_called(self, tmp_path: Path, catalog: SqliteCatalog):
        proc = _make_processor(tmp_path, catalog=catalog)
        # final_path
        final_path = tmp_path / "out" / "A" / "A - T.mp3"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"\xff\xfb" + b"\x00" * 100)
        result = FingerprintResult(artist="A", title="T", score=0.95, recording_id="r1")
        enriched = EnrichedInfo(artist="A", title="T", album="MyAlbum")
        deezer = DeezerData(rank=50000, isrc="ISRC1", cover_bytes=None, album="MyAlbum")
        with patch(
            "radio_ripper.services.processor.read_audio_from_file",
            return_value={"bitrate": 320, "sample_rate": 44100, "duration_ms": 200000},
        ):
            await proc._catalog_upsert(final_path, result, None, enriched, deezer, has_cover=True)
        records = await catalog.list_all()
        assert len(records) == 1
        r = records[0]
        assert r.recording_id == "r1" and r.isrc == "ISRC1"
        assert r.bitrate == 320 and r.popularity_rank == 50000
        assert r.album == "MyAlbum" and r.has_cover is True

    async def test_catalog_upsert_handles_audio_read_failure(self, tmp_path: Path, catalog: SqliteCatalog):
        proc = _make_processor(tmp_path, catalog=catalog)
        final_path = tmp_path / "out" / "A" / "A - T.mp3"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_bytes(b"\xff\xfb")
        result = FingerprintResult(artist="A", title="T", score=0.95, recording_id="r1")
        with patch("radio_ripper.services.processor.read_audio_from_file", side_effect=RuntimeError("boom")):
            await proc._catalog_upsert(final_path, result, None, None, None, has_cover=False)
        records = await catalog.list_all()
        assert len(records) == 1
        assert records[0].bitrate is None

    async def test_enrich_uses_catalog_candidates(self, tmp_path: Path, catalog: SqliteCatalog):
        """Enrich-Kandidaten kommen aus find_missing_tags (kein Datei-Scan)."""
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        from radio_ripper.services.tagging import ID3Tagger

        dest = tmp_path / "out"
        # Datei mit Artist/Title aber ohne Album/Genre/Cover (recording_id vorhanden)
        f = dest / "Artist" / "Artist - Title.mp3"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"\xff\xfb" + b"\x00" * 512)
        audio = ID3()
        audio.add(TPE1(encoding=3, text="Artist"))
        audio.add(TIT2(encoding=3, text="Title"))
        audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="rec-1"))
        audio.save(f, v2_version=3)

        # Katalog mit demselben Eintrag (fehlendes Album/Genre/Cover) befüllen
        await catalog.upsert(
            SongRecord(
                file_path=str(f),
                recording_id="rec-1",
                artist="Artist",
                title="Title",
                album=None,
                genre=None,
                has_cover=False,
            )
        )

        proc = _make_processor(tmp_path, catalog=catalog)
        proc._tagger = ID3Tagger()
        proc._metadata.fetch.return_value = EnrichedInfo(  # type: ignore[attr-defined]
            artist="Artist", title="Title", album="Great Album", genre="Rock"
        )
        proc._metadata.download_image = AsyncMock(return_value=None)  # type: ignore[method-assign]
        proc._cover_provider = AsyncMock()
        proc._cover_provider.fetch_cover_by_recording_id.return_value = None
        proc._cover_provider.fetch_recording_data.return_value = None

        await proc.enrich_existing_files()

        target = dest / "Artist" / "Great Album" / "Artist - Title.mp3"
        assert target.exists(), f"expected {target}, found: {list(dest.rglob('*.mp3'))}"
        t = ID3(target)
        assert t.get("TALB") is not None and t.get("TALB").text == ["Great Album"]
        assert t.get("TCON") is not None and t.get("TCON").text == ["Rock"]
        # Katalog-Eintrag nach Enrich aktualisiert (Genre + Album gesetzt)
        records = await catalog.list_all()
        new_records = [r for r in records if r.file_path == str(target)]
        assert len(new_records) == 1
        assert new_records[0].album == "Great Album"
        assert new_records[0].genre == "Rock"


class TestCriticalSectionEviction:
    """Bug 3 — _maybe_evict darf nur bei erfolgreich einsortierter Datei laufen."""

    async def test_no_eviction_when_file_rejected_by_collision(self, tmp_path: Path, catalog: SqliteCatalog):
        """Kollision am Ziel → Datei verworfen → KEIN Eviction (Sammlung ist nicht gewachsen)."""
        from radio_ripper.services.processor import CollectedMetadata

        proc = _make_processor(tmp_path, catalog=catalog, min_popularity_rank=0)
        proc._maybe_evict = AsyncMock()  # type: ignore[method-assign]
        proc._write_tags = AsyncMock(return_value=True)  # type: ignore[method-assign]
        proc._cover_provider = None
        proc._deezer_provider = None
        proc._popularity = None

        result = FingerprintResult(artist="Artist", title="Title", score=0.9, recording_id="r1")
        track = TrackInfo(stream_title="Artist - Title", artist="Artist", title="Title")
        meta = CollectedMetadata(
            result=result,
            track=track,
            enriched=None,
            mb_data=None,
            deezer_data=None,
            deezer_cover=None,
            cover_from_caa=None,
            cover_from_enrich=None,
            deezer_attempted=False,
            artist_image=None,
            lyrics=None,
        )
        # Ziel belegt von einem ANDEREN Song → Kollision
        dest = proc._settings.destination / "Artist"
        dest.mkdir(parents=True)
        existing = dest / "Artist - Title.mp3"
        existing.write_bytes(b"\xff\xfb" + b"\x00" * 512)
        from mutagen.id3 import ID3, TIT2, TPE1, TXXX

        a = ID3()
        a.add(TPE1(encoding=3, text="Artist"))
        a.add(TIT2(encoding=3, text="Title"))
        a.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text="other-rec"))
        a.save(existing, v2_version=3)

        work = tmp_path / "work" / "song.mp3"
        work.parent.mkdir(parents=True)
        work.write_bytes(b"\xff\xfb" + b"\x00" * 512)

        await proc._process_critical_section(meta=meta, work_path=work)
        proc._maybe_evict.assert_not_awaited()
        assert existing.exists()
