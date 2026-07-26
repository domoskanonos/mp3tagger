"""File processor — single-worker inbox processing for recorded MP3s.

Scans an ``inbox`` (mp3_inbox) for ``.mp3`` files and processes them one by
one: fingerprint → enrich → CAA → MB → popularity → lyrics → ONE tag write,
all in a ``work_dir`` staging area, then atomically moved to ``destination/``.
No database involved — files are either perfect (in destination/) or deleted.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from radio_ripper.domain.models import EnrichedInfo, MusicBrainzData, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.base_processor import BaseInboxProcessor
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.lyrics import LRCLibProvider
from radio_ripper.services.metadata import CoverArtProvider, MetadataProvider
from radio_ripper.services.popularity import PopularityProvider, maybe_delete_obscure
from radio_ripper.services.storage import (
    compute_file_path,
    read_acoustid_score,
    remove_empty_parents,
)
from radio_ripper.services.tagging import TrackTagger


class FileProcessor(BaseInboxProcessor):
    """Single-worker inbox processor.

    Polls *inbox* for ``.mp3`` files, processes each sequentially:
      1. Move ``.processing`` to ``work_dir/``.
      2. Fingerprint (AcoustID) — score < min → delete.
      3. Remux + enrich (iTunes) + basic tags.
      4. Score-compare with existing file at destination.
      5. CAA cover, MusicBrainz metadata, artist image, popularity.
      6. Lyrics.
      7. Atomic rename to ``destination/``.
    If ANY step fails the staging file is deleted; ``destination/`` is never
    touched until the file is fully processed.
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
        popularity_provider: PopularityProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(inbox, temp_dir, name=name, poll_interval=poll_interval, logger=logger)
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._tagger = tagger
        self._cover_provider = cover_provider
        self._popularity = popularity_provider
        self._null_repo = NullTrackRepository()

    async def _process_file(self, proc_path: Path) -> None:
        # ── Move .processing from inbox to work_dir for safe staging ──
        work_path = self._settings.work_dir / proc_path.name
        try:
            work_path.parent.mkdir(parents=True, exist_ok=True)
            proc_path.rename(work_path)
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben ins work_dir fehlgeschlagen",
                proc_path.name,
            )
            self._cleanup_file(proc_path)
            return

        # ── Fingerprint ──
        try:
            result = await self._fingerprint.fingerprint(work_path)
        except NonRetriableFingerprintError:
            self._log.warning("[DELETE] %s — Grund: Datei korrupt/nicht lesbar", work_path.name)
            self._cleanup_file(work_path)
            return
        except FingerprintError:
            self._log.warning(
                "[TEMP] %s — Grund: Fingerprint-Infrastrukturfehler, verschoben zur Prüfung",
                work_path.name,
            )
            self._move_to_temp(work_path)
            return

        if result is None or not result.recording_id:
            self._log.info("[DELETE] %s — Grund: kein AcoustID-Treffer", work_path.name)
            self._cleanup_file(work_path)
            return

        if result.score < self._settings.acoustid_min_score:
            self._log.info(
                "[DELETE] %s — Grund: Score %.2f < Mindestwert %.2f",
                work_path.name,
                result.score,
                self._settings.acoustid_min_score,
            )
            self._cleanup_file(work_path)
            return

        # ── TrackInfo aus Acoustic-Ergebnis ──
        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)

        # ── iTunes-Enrichment (nur Daten holen, kein Tag-Schreiben) ──
        enriched: EnrichedInfo | None = None
        cover_from_enrich: bytes | None = None
        try:
            enriched = await self._metadata.fetch(result.artist, result.title)
            if enriched and enriched.artwork_url:
                cover_from_enrich = await self._metadata.download_image(enriched.artwork_url)
        except Exception:
            self._log.debug("[%s] iTunes enrichment failed for %s", self._name, work_path.name)

        # ── Ziel-Pfad berechnen ──
        provenance = f"{self._name}/{self._name}"
        album = enriched.album if enriched and enriched.album else None
        final_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
            album=album,
        )

        # ── Score-basierte Entscheidung ──
        delete_old: Path | None = None
        if final_path.exists():
            existing_score = read_acoustid_score(final_path)
            if existing_score is not None and existing_score >= result.score:
                self._log.info(
                    "[DELETE] %s — Grund: existierende Datei hat besseren/gleichen "
                    "Score (%.4f >= %.4f)",
                    work_path.name,
                    existing_score,
                    result.score,
                )
                self._cleanup_file(work_path)
                return
            self._log.info(
                "Neue Datei hat besseren Score (%.4f > %.4f) — "
                "alte wird nach erfolgreichem Move gelöscht",
                result.score,
                existing_score or 0.0,
            )
            delete_old = final_path

        # ── CAA, MusicBrainz, Künstlerbild (kein Tag-Schreiben) ──
        mb_data: MusicBrainzData | None = None
        cover_from_caa: bytes | None = None
        artist_image: bytes | None = None
        if self._cover_provider and result.recording_id:
            tasks: list = [
                self._cover_provider.fetch_cover_by_recording_id(result.recording_id),
                self._cover_provider.fetch_recording_data(result.recording_id),
            ]
            if self._popularity and result.artist:
                tasks.append(self._popularity.fetch_artist_image(result.artist))
            cov_results = await asyncio.gather(*tasks, return_exceptions=True)
            cover_from_caa = cov_results[0] if not isinstance(cov_results[0], BaseException) else None
            mb_data = cov_results[1] if not isinstance(cov_results[1], BaseException) else None
            if len(cov_results) > 2:
                artist_image = cov_results[2] if not isinstance(cov_results[2], BaseException) else None

        # ── Rename .processing → .mp3 ──
        stage_path = work_path
        new_path = work_path.with_name(work_path.stem.replace(".untested", "") + ".mp3")
        if new_path != work_path:
            if new_path.exists():
                self._log.warning(
                    "[%s] Refuse to rename %s -> %s (target exists).",
                    self._name, work_path.name, new_path.name,
                )
                self._cleanup_file(work_path)
                return
            try:
                work_path.rename(new_path)
                stage_path = new_path
            except OSError as exc:
                self._log.warning("[%s] rename failed: %s", self._name, exc)
                self._cleanup_file(work_path)
                return

        # ── Popularität (Deezer) – löscht Datei bei zu unbekannt ──
        if self._settings.min_popularity_rank > 0 and self._popularity and (result.artist or result.title):
            deleted = await maybe_delete_obscure(
                file_path=stage_path,
                station_name=self._name,
                stream_title=stream_title,
                artist=result.artist,
                title=result.title,
                min_rank=self._settings.min_popularity_rank,
                popularity_provider=self._popularity,
                repository=self._null_repo,
                logger=self._log,
            )
            if deleted:
                return

        # ── Lyrics ──
        lyrics: str | None = None
        try:
            lyrics_provider = LRCLibProvider(HttpxAsyncClient(), timeout=5.0)
            lyrics = await lyrics_provider.fetch(track.artist, track.title)
        except Exception:
            self._log.debug("[%s] Lyrics fetch failed for %s", self._name, stage_path.name)

        # ── EIN Tag-Schreib-Durchgang ──
        final_cover = cover_from_caa or cover_from_enrich
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
            )
            if final_cover is not None:
                self._log.info("[%s] Cover embedded: %s", self._name, stage_path.name)
            if lyrics:
                self._log.info(
                    "[%s] Lyrics found for %s (%d chars)",
                    self._name, stage_path.name, len(lyrics),
                )
        except Exception as exc:
            self._log.warning("[%s] Tag write failed: %s", self._name, exc)

        # ── Atomarer Move zu destination ──
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stage_path.rename(final_path)
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben nach %s fehlgeschlagen",
                stage_path.name, final_path,
            )
            self._cleanup_file(stage_path)
            return

        self._log.info("[%s] Fertig: %s", self._name, final_path)

        # ── Alte Datei erst jetzt löschen (neue ist sicher am Ziel) ──
        if delete_old is not None:
            self._log.info(
                "[DELETE] %s — Grund: durch bessere Version ersetzt",
                delete_old.name,
            )
            delete_old.unlink(missing_ok=True)
            remove_empty_parents(delete_old, self._settings.destination)

    async def _on_processing_error(self, proc_path: Path) -> None:
        self._cleanup_file(proc_path)


__all__ = ["FileProcessor"]
