"""File processor — single-worker inbox processing for recorded MP3s.

Scans an ``inbox`` (mp3_inbox) for ``.mp3`` files and processes them one by
one: fingerprint → remux → enrich → tag → score-compare → atomic move to
destination → CAA → MB metadata → artist image → popularity → lyrics.
No database involved — files are either perfect (in destination/) or deleted.
"""

from __future__ import annotations

import logging
from pathlib import Path

from radio_ripper.domain.models import FingerprintResult, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.base_processor import BaseInboxProcessor
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.lyrics import LyricsOvhProvider
from radio_ripper.services.metadata import CoverArtProvider, MetadataProvider
from radio_ripper.services.popularity import PopularityProvider
from radio_ripper.services.repository import NullTrackRepository
from radio_ripper.services.storage import (
    compute_file_path,
    read_acoustid_score,
    remux_mp3,
)
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.track_processing import (
    enrich_and_file,
    fingerprint_song,
)


class FileProcessor(BaseInboxProcessor):
    """Single-worker inbox processor.

    Polls *inbox* for ``.mp3`` files, processes each sequentially:
      1. Fingerprint (AcoustID) — score < min → delete.
      2. Remux + enrich (iTunes) + basic tags.
      3. Score-compare with existing file at destination.
      4. Atomic move to ``destination/`` (incl. album subfolder).
      5. CAA cover, MusicBrainz metadata, artist image, lyrics.
    Any failure during steps 1–3 → file deleted.
    Failure after the atomic move is logged; the file already lands in
    ``destination/`` (tags are re-applied on a subsequent run).
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
        try:
            result = await self._fingerprint.fingerprint(proc_path)
        except NonRetriableFingerprintError:
            self._log.warning("[DELETE] %s — Grund: Datei korrupt/nicht lesbar", proc_path.name)
            self._cleanup_file(proc_path)
            return
        except FingerprintError:
            self._log.warning(
                "[TEMP] %s — Grund: Fingerprint-Infrastrukturfehler, verschoben zur Prüfung",
                proc_path.name,
            )
            self._move_to_temp(proc_path)
            return

        if result is None or not result.recording_id:
            self._log.info("[DELETE] %s — Grund: kein AcoustID-Treffer", proc_path.name)
            self._cleanup_file(proc_path)
            return

        if result.score < self._settings.acoustid_min_score:
            self._log.info(
                "[DELETE] %s — Grund: Score %.2f < Mindestwert %.2f",
                proc_path.name,
                result.score,
                self._settings.acoustid_min_score,
            )
            self._cleanup_file(proc_path)
            return

        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)
        provenance = f"{self._name}/{self._name}"

        # Remux in-place on .processing
        remux_mp3(proc_path)

        # Enrich + basic tags (in-place on .processing)
        info = await enrich_and_file(
            proc_path,
            track,
            self._name,
            provenance,
            self._settings,
            self._tagger,
            metadata_provider=self._metadata,
            logger=self._log,
        )

        # Compute final destination path (with album subfolder if available)
        album = info.album if info and info.album else None
        final_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
            album=album,
        )

        # Score-based overwrite decision
        if final_path.exists():
            existing_score = read_acoustid_score(final_path)
            if existing_score is not None and existing_score >= result.score:
                self._log.info(
                    "[DELETE] %s — Grund: existierende Datei hat besseren/gleichen Score (%.4f >= %.4f)",
                    proc_path.name,
                    existing_score,
                    result.score,
                )
                self._cleanup_file(proc_path)
                return
            self._log.info(
                "[DELETE] %s — Grund: neue Datei hat besseren Score (%.4f > %.4f), alte gelöscht",
                final_path,
                result.score,
                existing_score or 0.0,
            )
            final_path.unlink(missing_ok=True)

        # Atomic move to final destination
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc_path.rename(final_path)
        except OSError:
            self._log.error("[DELETE] %s — Grund: Verschieben nach %s fehlgeschlagen", proc_path.name, final_path)
            self._cleanup_file(proc_path)
            return

        self._log.info("[%s] Moved to destination: %s", self._name, final_path)

        # Post-move: CAA, MB metadata, artist image, popularity, lyrics
        await fingerprint_song(
            final_path,
            track,
            self._name,
            provenance,
            self._settings,
            self._fingerprint,
            self._null_repo,
            self._tagger,
            cover_provider=self._cover_provider,
            popularity_provider=self._popularity,
            logger=self._log,
            precomputed_result=result,
        )

        try:
            lyrics_provider = LyricsOvhProvider(HttpxAsyncClient(), timeout=5.0)
            lyrics = await lyrics_provider.fetch(track.artist, track.title)
            if lyrics:
                self._tagger.write_lyrics(final_path, lyrics)
                self._log.info(
                    "[%s] Lyrics found for %s (%d chars)",
                    self._name,
                    final_path.name,
                    len(lyrics),
                )
        except Exception:
            self._log.debug("[%s] Lyrics fetch failed for %s", self._name, final_path.name)

    async def _on_processing_error(self, proc_path: Path) -> None:
        self._cleanup_file(proc_path)


__all__ = ["FileProcessor"]
