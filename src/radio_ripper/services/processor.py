"""File processor — concurrent inbox processing for recorded MP3s.

Scans an ``inbox`` (mp3_inbox) for ``.mp3`` files and processes up to
``max_concurrent`` of them in parallel. Per file:
fingerprint → [CAA+MB || iTunes+Lyrics+ArtImg || Deezer] (parallel) →
MB-Korrektur → Score-Vergleich → Popularitäts-Prüfung → ONE tag write,
all in a ``work_dir`` staging area, then atomically moved to ``destination/``.
No database involved — files are either perfect (in destination/) or deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import EnrichedInfo, FingerprintResult, MusicBrainzData, TrackInfo
from radio_ripper.infra.catalog import Catalog, SongRecord, read_audio_from_file
from radio_ripper.infra.config import Settings
from radio_ripper.services.collection_manager import (
    is_better_version,
    is_same_version,
    pick_eviction_candidate,
    should_exclude_as_live,
)
from radio_ripper.services.file_utils import compute_file_path, safe_unlink
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.lyrics import LyricsProvider
from radio_ripper.services.metadata_deezer import DeezerData, DeezerMetadataProvider
from radio_ripper.services.metadata_itunes import MetadataProvider
from radio_ripper.services.metadata_musicbrainz import CoverArtProvider
from radio_ripper.services.popularity import PopularityProvider
from radio_ripper.services.tagging import TrackTagger, read_acoustid_score

# ── helpers ──


def _strip_untested_suffix(
    file_path: Path,
    logger: logging.Logger,
    station_name: str,
    *,
    on_fail: str | None = None,
) -> Path | None:
    """Entfernt den ``.untested``-Suffix aus einer Datei.

    AcoustID hängt ``.untested`` an Dateien an, die noch nicht gematcht wurden.
    Dieses benennt sie zurück zu ``.mp3``, sobald ein Match vorliegt.
    """
    new_path = file_path.with_name(file_path.stem.replace(".untested", "") + ".mp3")
    if file_path == new_path:
        return file_path
    if new_path.exists():
        logger.warning(
            "[%s] Refuse to rename %s -> %s (target exists).%s",
            station_name,
            file_path.name,
            new_path.name,
            on_fail or " Keeping .untested.mp3 for manual review.",
        )
        return None
    try:
        file_path.rename(new_path)
        return new_path
    except OSError as exc:
        logger.warning(
            "[%s] rename %s -> %s failed: %s",
            station_name,
            file_path.name,
            new_path.name,
            exc,
        )
        return None


def correct_fingerprint_result(
    result: FingerprintResult,
    mb_data: MusicBrainzData | None,
) -> FingerprintResult:
    """Überschreibt AcoustID-Artist/Title mit MusicBrainz-Daten, wenn vorhanden.

    AcoustID liefert oft nur grobe Metadaten oder hat Künstler/Titel
    vertauscht. MusicBrainz ist die kanonische Quelle — wenn MB-Daten
    existieren, werden sie immer bevorzugt.
    """
    if mb_data and mb_data.recording_artist:
        return FingerprintResult(
            artist=mb_data.recording_artist,
            title=mb_data.recording_title or result.title,
            score=result.score,
            recording_id=result.recording_id,
        )
    return result


async def _fetch_artist_image(
    popularity_provider: PopularityProvider,
    artist: str,
    station_name: str,
    logger: logging.Logger,
) -> bytes | None:
    try:
        img = await popularity_provider.fetch_artist_image(artist)
        if img is not None:
            return img
    except Exception as exc:
        logger.debug("[%s] artist image fetch failed: %s", station_name, exc)
    return None


async def _fetch_cover_data(
    cover_provider: CoverArtProvider,
    recording_id: str,
    popularity_provider: PopularityProvider | None,
    artist: str,
    station_name: str,
    logger: logging.Logger,
) -> tuple[bytes | None, MusicBrainzData | None, bytes | None]:
    """Holt Cover (CAA), MB-Metadaten und Künstlerbild parallel ab.

    Alle drei API-Aufrufe sind unabhängig und laufen gleichzeitig.
    Fehler werden einzeln abgefangen — ein fehlschlagender Aufruf
    blockiert die anderen nicht.
    """

    async def _fetch_cover() -> bytes | None:
        try:
            return await cover_provider.fetch_cover_by_recording_id(recording_id)
        except Exception as exc:
            logger.debug("[%s] CAA cover lookup failed: %s", station_name, exc)
            return None

    async def _fetch_mb() -> MusicBrainzData | None:
        try:
            d = await cover_provider.fetch_recording_data(recording_id)
            if d and d.release_label:
                logger.info("[%s] MB label: %s", station_name, d.release_label)
            return d
        except Exception as exc:
            logger.debug("[%s] MB recording data lookup failed: %s", station_name, exc)
            return None

    tasks = [_fetch_cover(), _fetch_mb()]
    if popularity_provider is not None and artist:
        tasks.append(_fetch_artist_image(popularity_provider, artist, station_name, logger))
    raw: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)
    cover_b: bytes | None = None if isinstance(raw[0], BaseException) else raw[0]
    mb: MusicBrainzData | None = None if isinstance(raw[1], BaseException) else raw[1]
    art: bytes | None = None
    if len(raw) > 2 and not isinstance(raw[2], BaseException):
        art = raw[2]
    return cover_b, mb, art


async def _fetch_lyrics(
    provider: LyricsProvider,
    artist: str,
    title: str,
    logger: logging.Logger,
    name: str,
) -> str | None:
    try:
        return await provider.fetch(artist, title)
    except Exception as exc:
        logger.debug("[%s] Lyrics fetch failed: %s", name, exc)
        return None


# ── processor ──


class FileProcessor:
    """Concurrent inbox processor (Semaphore-begrenzt, ``max_concurrent``).

    Pollt *inbox* nach ``.mp3``-Dateien und verarbeitet bis zu
    ``max_concurrent`` parallel. Pro Datei:

      1. Move ``.processing`` → ``work_dir/``.
      2. Fingerprint (AcoustID) — Score < min → löschen.
      3. Parallel: CAA+MB || iTunes+Lyrics+ArtImg || Deezer.
      4. MB-Korrektur (artist/title) — ggf. Deezer re-fetch.
      5. Score-Vergleich mit bestehender Datei im Destination.
      6. ``.untested`` → ``.mp3`` umbenennen.
      7. Popularitäts-Prüfung über den Deezer-Rang (gleicher API-Call
         wie Phase 3, kein extra Request). Datei wird gelöscht wenn
         Deezer sie nicht kennt oder der Rang unter ``min_popularity_rank``
         liegt.
      8. One-Pass Tag-Write (Cover-Prio: Deezer → CAA → iTunes).
      9. Atomarer Move nach ``destination/``; alte Datei wird bei
         höherem Score ersetzt.

    Wenn ein Schritt scheitert, wird die Stage-Datei gelöscht;
    ``destination/`` wird erst nach vollständigem Erfolg berührt.
    """

    def __init__(
        self,
        inbox: Path,
        temp_dir: Path,
        settings: Settings,
        fingerprint_provider: FingerprintProvider,
        metadata_provider: MetadataProvider,
        tagger: TrackTagger,
        *,
        name: str = "processor",
        poll_interval: float = 5.0,
        cover_provider: CoverArtProvider | None = None,
        deezer_provider: DeezerMetadataProvider | None = None,
        popularity_provider: PopularityProvider | None = None,
        lyrics_provider: LyricsProvider | None = None,
        catalog: Catalog | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._name = name
        self._poll_interval = poll_interval
        self._log = logger or logging.getLogger(f"radio_ripper.{name}")
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._tagger = tagger
        self._cover_provider = cover_provider
        self._deezer_provider = deezer_provider
        self._popularity = popularity_provider
        self._lyrics_provider = lyrics_provider
        self._catalog = catalog
        self._semaphore = asyncio.Semaphore(settings.max_concurrent)

    # ── public lifecycle ──

    async def start(self) -> None:
        self._inbox.mkdir(parents=True, exist_ok=True)
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ── polling loop ──

    async def _run(self) -> None:
        self._log.info(
            "%s started — polling %s every %.0fs",
            self._name.title(),
            self._inbox,
            self._poll_interval,
        )
        while not self._stop_event.is_set():
            try:
                await self._drain_inbox()
            except Exception:
                self._log.exception("%s inbox scan failed", self._name.title())
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
        self._log.info("%s stopped", self._name.title())

    async def _drain_inbox(self) -> None:
        mp3s = sorted(self._inbox.glob("*.mp3"))
        if not mp3s:
            return

        async def _gated_process(mp3: Path) -> None:
            async with self._semaphore:
                if self._stop_event.is_set():
                    return
                try:
                    await self._process_one(mp3)
                except Exception:
                    self._log.exception("Unexpected error processing %s", mp3)

        tasks = [asyncio.create_task(_gated_process(mp3)) for mp3 in mp3s]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_one(self, mp3_path: Path) -> None:
        proc_path = mp3_path.with_suffix(".processing")
        try:
            mp3_path.rename(proc_path)
        except OSError:
            self._log.warning("Cannot rename %s (concurrent access?) — skipping", mp3_path)
            return

        try:
            await self._process_file(proc_path)
        except Exception:
            self._log.exception("Failed to process %s", proc_path.name)
            self._cleanup_file(proc_path)

    # ── helpers ──

    def _move_to_temp(self, path: Path) -> None:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        dest = self._temp_dir / path.name
        if dest.suffix == ".processing":
            dest = dest.with_suffix(".mp3")
        try:
            shutil.move(str(path), str(dest))
            self._log.info("Moved %s → %s", path.name, dest)
        except OSError:
            self._log.exception("Failed to move %s to temp", path)

    def _cleanup_file(self, path: Path) -> None:
        safe_unlink(path)

    # ── Hauptverarbeitungsschritte ──

    async def _move_to_work_dir(self, proc_path: Path) -> Path | None:
        """Verschiebt die .processing-Datei aus dem Inbox ins work_dir."""
        work_path = self._settings.work_dir / proc_path.name
        try:
            work_path.parent.mkdir(parents=True, exist_ok=True)
            proc_path.rename(work_path)
            return work_path
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben ins work_dir fehlgeschlagen",
                proc_path.name,
            )
            self._cleanup_file(proc_path)
            return None

    async def _fingerprint_and_validate(self, work_path: Path) -> FingerprintResult | None:
        """Fingerprint + Fehlerbehandlung. Gibt None bei Abbruch."""
        try:
            result = await self._fingerprint.fingerprint(work_path)
        except NonRetriableFingerprintError:
            self._log.warning("[DELETE] %s — Grund: Datei korrupt/nicht lesbar", work_path.name)
            self._cleanup_file(work_path)
            return None
        except FingerprintError:
            self._log.warning(
                "[TEMP] %s — Grund: Fingerprint-Infrastrukturfehler, verschoben zur Prüfung",
                work_path.name,
            )
            self._move_to_temp(work_path)
            return None

        if result is None or not result.recording_id:
            self._log.info("[DELETE] %s — Grund: kein AcoustID-Treffer", work_path.name)
            self._cleanup_file(work_path)
            return None

        if result.score < self._settings.acoustid_min_score:
            self._log.info(
                "[DELETE] %s — Grund: Score %.2f < Mindestwert %.2f",
                work_path.name,
                result.score,
                self._settings.acoustid_min_score,
            )
            self._cleanup_file(work_path)
            return None

        return result

    async def _enrich_parallel(
        self,
        result: FingerprintResult,
        track: TrackInfo,
        work_path: Path,
    ) -> tuple[EnrichedInfo | None, bytes | None, bytes | None, str | None]:
        """Führt iTunes, Lyrics und Artist-Image parallel aus."""
        enriched: EnrichedInfo | None = None
        cover_from_enrich: bytes | None = None
        artist_image: bytes | None = None
        lyrics: str | None = None

        async def _fetch_itunes() -> None:
            nonlocal enriched, cover_from_enrich
            try:
                enriched = await self._metadata.fetch(result.artist, result.title)
                if enriched and enriched.artwork_url:
                    cover_from_enrich = await self._metadata.download_image(enriched.artwork_url)
            except Exception:
                self._log.debug("[%s] iTunes enrichment failed for %s", self._name, work_path.name)

        async def _fetch_lyr() -> None:
            nonlocal lyrics
            if self._lyrics_provider is None:
                return
            lyrics = await _fetch_lyrics(
                self._lyrics_provider,
                track.artist,
                track.title,
                self._log,
                self._name,
            )

        async def _fetch_art_img() -> None:
            nonlocal artist_image
            if not self._popularity or not result.artist:
                return
            artist_image = await _fetch_artist_image(
                self._popularity,
                result.artist,
                self._name,
                self._log,
            )

        await asyncio.gather(_fetch_itunes(), _fetch_lyr(), _fetch_art_img())
        return enriched, cover_from_enrich, artist_image, lyrics

    async def _compute_destination_and_score(
        self,
        result: FingerprintResult,
        track: TrackInfo,
        enriched: EnrichedInfo | None,
        work_path: Path,
        *,
        mb_data: MusicBrainzData | None = None,
    ) -> tuple[str, Path | None, Path | None]:
        """Berechnet Zielpfad + Versions-Vergleich mit bestehenden Dateien.

        Returns (provenance, final_path, delete_old).
        delete_old ist None wenn keine alte Datei ersetzt wird.

        Priorität:
          1. Katalog-Abfrage via ``recording_id`` — falls gleiche Version
             (gleiche MBID + gleiche ISRC) vorliegt, vergleiche Score/Bitrate
             per :func:`is_better_version` und ersetze die schlechtere Datei.
          2. Pfad-Kollision (gleicher Zielpfad) — Score-Vergleich via ID3
             (Legacy-Verhalten, Fallback für Dateien ohne ISRC).
        """
        provenance = f"{self._name}/{self._name}"
        album = enriched.album if enriched and enriched.album else None
        final_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            track.stream_title,
            album=album,
        )

        delete_old: Path | None = None

        # ── Schritt 1: Katalog-basierter Versionsvergleich ──
        if self._catalog is not None and result.recording_id:
            existing_versions = await self._catalog.find_by_recording_id(result.recording_id)
            new_isrc = mb_data.isrcs[0] if mb_data and mb_data.isrcs else None
            for ev in existing_versions:
                if not is_same_version(result.recording_id, new_isrc, ev.recording_id, ev.isrc):
                    continue
                if ev.file_path == str(final_path):
                    continue  # wird über Pfad-Fallback behandelt
                # Gleiche Version — Audioqualität vergleichen (neu ist noch unkatalogisiert,
                # aber wir haben dessen Fingerprint-Score; Bitrate erst nach Tagging verfügbar).
                if is_better_version(
                    result.score, None, None,
                    ev.acoustid_score, ev.bitrate, ev.sample_rate,
                ):
                    candidate = Path(ev.file_path)
                    if candidate.exists():
                        delete_old = candidate
                        self._log.info(
                            "[%s] Katalog-Treffer: ersetze %s (score=%.4f) durch bessere Version (%.4f)",
                            self._name,
                            candidate.name,
                            ev.acoustid_score or 0.0,
                            result.score,
                        )
                        break
                # bestehende Version hat besseren Score → neue verwerfen
                self._log.info(
                    "[DELETE] %s — Grund: Katalog-Treffer mit gleicher/ besserer Version (score=%.4f >= %.4f)",
                    work_path.name,
                    ev.acoustid_score or 0.0,
                    result.score,
                )
                return "", None, None

        # ── Schritt 2: Pfad-Fallback (gleicher Zielpfad) ──
        if final_path.exists() and delete_old is None:
            existing_score = read_acoustid_score(final_path)
            if existing_score is not None and existing_score >= result.score:
                self._log.info(
                    "[DELETE] %s — Grund: existierende Datei hat besseren/gleichen Score (%.4f >= %.4f)",
                    work_path.name,
                    existing_score,
                    result.score,
                )
                return "", None, None
            self._log.info(
                "Neue Datei hat besseren Score (%.4f > %.4f) — alte wird nach erfolgreichem Move gelöscht",
                result.score,
                existing_score or 0.0,
            )
            delete_old = final_path

        return provenance, final_path, delete_old

    async def _process_file(self, proc_path: Path) -> None:
        work_path = await self._move_to_work_dir(proc_path)
        if work_path is None:
            return

        result = await self._fingerprint_and_validate(work_path)
        if result is None:
            return

        # ── Phase 3: CAA+MB || iTunes+Lyrics+ArtImg || Deezer (alle parallel, alle nur vom Fingerprint abhängig) ──
        fp_result = result
        stream_title_pre = f"{fp_result.artist} - {fp_result.title}"
        track_pre = TrackInfo.from_stream_title(stream_title_pre)

        # deezer_attempted unterscheidet "Deezer angerufen, kein Treffer" (→ löschen)
        # von "Deezer gar nicht versucht / Exception" (→ überspringen, API evtl. down)
        deezer_attempted = False

        async def _phase1_caa_mb() -> tuple[bytes | None, MusicBrainzData | None]:
            if not self._cover_provider or not fp_result.recording_id:
                return None, None
            cover, mb, _ = await _fetch_cover_data(
                self._cover_provider,
                fp_result.recording_id,
                None,
                "",
                self._name,
                self._log,
            )
            return cover, mb

        async def _phase_deezer() -> tuple[DeezerData | None, bytes | None]:
            nonlocal deezer_attempted
            if not self._deezer_provider or not (fp_result.artist or fp_result.title):
                return None, None
            try:
                data = await self._deezer_provider.fetch(fp_result.artist, fp_result.title)
                deezer_attempted = True
                cover = data.cover_bytes if data else None
                return data, cover
            except Exception:
                self._log.debug("[%s] Deezer fetch failed", self._name)
                return None, None

        phase1_task = asyncio.create_task(_phase1_caa_mb())
        phase3_task = asyncio.create_task(self._enrich_parallel(fp_result, track_pre, work_path))
        deezer_task = asyncio.create_task(_phase_deezer())
        cover_from_caa, mb_data = await phase1_task
        enriched, cover_from_enrich, artist_image, lyrics = await phase3_task
        deezer_data, deezer_cover = await deezer_task

        # ── Phase 4: MB-Korrektur (MB-Daten sind kanonisch) ──
        corrected = correct_fingerprint_result(result, mb_data)
        if corrected is not result:
            self._log.info(
                "[%s] MB corrected artist/title: %s -> %s / %s -> %s",
                self._name,
                result.artist,
                corrected.artist,
                result.title,
                corrected.title,
            )
            # MB hat artist/title geändert → Deezer war für "falschen" Künstler gesucht → neu fetchen
            if (
                deezer_data is not None
                and (corrected.artist != fp_result.artist or corrected.title != fp_result.title)
                and self._deezer_provider is not None
            ):
                try:
                    deezer_data = await self._deezer_provider.fetch(corrected.artist, corrected.title)
                    deezer_attempted = True
                    deezer_cover = deezer_data.cover_bytes if deezer_data else None
                except Exception:
                    self._log.debug("[%s] Deezer re-fetch after MB correction failed", self._name)
                    deezer_data = None
                    deezer_cover = None
            result = corrected
        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)

        # ── Phase 5: Zielpfad + Score-Entscheidung (Katalog-basiert + Pfad-Fallback) ──
        provenance, final_path, delete_old = await self._compute_destination_and_score(
            result,
            track,
            enriched,
            work_path,
            mb_data=mb_data,
        )
        if final_path is None:  # bestehende Datei hat besseren Score
            self._cleanup_file(work_path)
            return

        # ── Phase 6: .untested → .mp3 umbenennen ──
        stage_path = _strip_untested_suffix(work_path, self._log, self._name, on_fail="")
        if stage_path is None:
            self._cleanup_file(work_path)
            return

        # ── Phase 7: Live-Ausschluss + Popularitäts-Prüfung (Deezer-Rank) ──
        release_group_type = mb_data.release_group_type if mb_data else None
        if should_exclude_as_live(
            release_group_type,
            result.title,
            self._settings.exclude_release_group_types,
            self._settings.exclude_title_patterns,
        ):
            self._log.info(
                "[DELETE] %s — Grund: Live/Bootleg ausgeschlossen (rg_type=%s, title=%s)",
                stage_path.name,
                release_group_type,
                result.title,
            )
            safe_unlink(stage_path)
            return

        if self._settings.min_popularity_rank > 0 and (result.artist or result.title):
            if deezer_attempted and deezer_data is None:
                # Deezer angerufen, 0 Treffer → "nicht auf Deezer" → löschen
                safe_unlink(stage_path)
                self._log.warning(
                    "[%s] Deleted unknown track (not on Deezer): %s",
                    self._name,
                    stage_path.name,
                )
                return
            if not deezer_attempted:
                # Deezer call fehlgeschlagen (API down) oder Provider fehlt → nicht löschen
                self._log.debug("[%s] Skip popularity check (Deezer not attempted)", self._name)
            elif deezer_data is not None:
                rank = deezer_data.rank
                if rank is None:
                    # Sollte nicht passieren, aber defensiv
                    self._log.warning(
                        "[%s] Deezer treffer ohne rank? Behalte: %s",
                        self._name,
                        stage_path.name,
                    )
                elif rank < self._settings.min_popularity_rank:
                    safe_unlink(stage_path)
                    self._log.warning(
                        "[%s] Deleted unpopular track (rank=%d < min=%d): %s",
                        self._name,
                        rank,
                        self._settings.min_popularity_rank,
                        stage_path.name,
                    )
                    return
                else:
                    self._log.info(
                        "[%s] Popularity rank OK — %s / %s = %d",
                        self._name,
                        result.artist,
                        result.title,
                        rank,
                    )

        # ── Phase 8: Tag-Write (Cover: Deezer → CAA → iTunes) ──
        final_cover = deezer_cover or cover_from_caa or cover_from_enrich

        try:
            self._tagger.write_all(
                stage_path,
                track,
                provenance,
                enriched=enriched,
                cover_bytes=final_cover,
                recording_id=result.recording_id,
                score=result.score,
                mb_data=mb_data,
                artist_image=artist_image,
                lyrics=lyrics,
                deezer=deezer_data,
            )
            if final_cover is not None:
                self._log.info("[%s] Cover embedded: %s", self._name, stage_path.name)
            if lyrics:
                self._log.info(
                    "[%s] Lyrics found for %s (%d chars)",
                    self._name,
                    stage_path.name,
                    len(lyrics),
                )
        except Exception as exc:
            self._log.warning("[%s] Tag write failed: %s", self._name, exc)

        # ── Phase 9: Atomarer Move zu destination + ggf. alte Datei löschen ──
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stage_path.rename(final_path)
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben nach %s fehlgeschlagen",
                stage_path.name,
                final_path,
            )
            self._cleanup_file(stage_path)
            return

        self._log.info("[%s] Fertig: %s", self._name, final_path)

        if delete_old is not None:
            self._log.info(
                "[DELETE] %s — Grund: durch bessere Version ersetzt",
                delete_old.name,
            )
            safe_unlink(delete_old, parents_root=self._settings.destination)
            if self._catalog is not None:
                await self._catalog.remove(str(delete_old))

        # ── Phase 10: Katalog-Upsert + Eviction ──
        if self._catalog is not None:
            await self._catalog_upsert(
                final_path, result, mb_data, enriched, deezer_data, has_cover=final_cover is not None,
            )
            await self._maybe_evict(deezer_data.rank if deezer_data else None)

    async def _catalog_upsert(
        self,
        final_path: Path,
        result: FingerprintResult,
        mb_data: MusicBrainzData | None,
        enriched: EnrichedInfo | None,
        deezer_data: DeezerData | None,
        *,
        has_cover: bool,
    ) -> None:
        """Liest Audio-Eigenschaften nach dem Move und trägt das Lied im Katalog ein."""
        assert self._catalog is not None
        try:
            audio = await asyncio.to_thread(read_audio_from_file, final_path)
        except Exception:
            self._log.debug("[%s] Audio-Lesen für Katalog fehlgeschlagen: %s", self._name, final_path)
            audio = {"bitrate": None, "sample_rate": None, "duration_ms": None}
        isrc = None
        if deezer_data and deezer_data.isrc:
            isrc = deezer_data.isrc
        elif mb_data and mb_data.isrcs:
            isrc = mb_data.isrcs[0]
        rec = SongRecord(
            file_path=str(final_path),
            recording_id=result.recording_id,
            isrc=isrc,
            artist=result.artist,
            title=result.title,
            album=enriched.album if enriched else None,
            release_group_type=mb_data.release_group_type if mb_data else None,
            station_name=self._name,
            file_size=final_path.stat().st_size if final_path.exists() else None,
            bitrate=audio["bitrate"],
            sample_rate=audio["sample_rate"],
            duration_ms=audio["duration_ms"],
            acoustid_score=result.score,
            popularity_rank=deezer_data.rank if deezer_data else None,
            has_cover=has_cover,
        )
        try:
            await self._catalog.upsert(rec)
        except Exception:
            self._log.debug("[%s] Katalog-Upsert fehlgeschlagen: %s", self._name, final_path)

    async def _maybe_evict(self, new_rank: int | None) -> None:
        """Verdrängt das unpopulärste Lied, wenn das Sammlungslimit erreicht ist."""
        if not self._catalog or not self._settings.enable_eviction:
            return
        if self._settings.max_collection_size <= 0 or new_rank is None:
            return
        try:
            current = await self._catalog.count()
        except Exception:
            self._log.debug("[%s] Katalog-Count fehlgeschlagen", self._name)
            return
        if current < self._settings.max_collection_size:
            return
        try:
            candidates = await self._catalog.find_least_popular(limit=20)
        except Exception:
            self._log.debug("[%s] find_least_popular fehlgeschlagen", self._name)
            return
        victim = pick_eviction_candidate(candidates, new_rank)
        if victim is None:
            return
        victim_path = Path(victim.file_path)
        self._log.info(
            "[EVICT] %s — Grund: Rank %d < neuer Rank %d (Sammlung bei Limit %d)",
            victim.file_path,
            victim.popularity_rank,
            new_rank,
            self._settings.max_collection_size,
        )
        safe_unlink(victim_path, parents_root=self._settings.destination)
        try:
            await self._catalog.remove(victim.file_path)
        except Exception:
            self._log.debug("[%s] Katalog-Remove nach Eviction fehlgeschlagen", self._name)


__all__ = ["FileProcessor"]
