"""Inbox-based MP3 uploader — thin orchestrator reusing the shared pipeline.

Scans *mp3_inbox* for ``.mp3`` files, fingerprints them to determine
identity, then runs them through the same post-processing pipeline as
live-stream recordings (register, enrich, tag, album-move, CAA cover,
popularity check, cross-station dedup).
"""

from __future__ import annotations

import logging
from pathlib import Path

from radio_ripper.domain.models import TrackInfo
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
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import (
    compute_file_path,
    read_acoustid_score,
    remove_empty_parents,
)
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.track_processing import (
    fingerprint_song,
    register_and_enrich,
)


class Uploader(BaseInboxProcessor):
    """Scans *mp3_inbox* for ``.mp3`` files, processes each through
    fingerprint → shared pipeline (register, enrich, tag, album-move,
    CAA cover, popularity, dedup).

    Files that match a known recording are routed to ``destination/``.
    Unmatched or failed files are moved to *temp_dir*.
    Corrupt files are deleted.
    """

    def __init__(
        self,
        inbox: Path,
        temp_dir: Path,
        settings: Settings,
        fingerprint_provider: FingerprintProvider,
        metadata_provider: MetadataProvider,
        repository: TrackRepository,
        tagger: TrackTagger,
        *,
        name: str = "inbox",
        poll_interval: float = 60.0,
        cover_provider: CoverArtProvider | None = None,
        popularity_provider: PopularityProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        super().__init__(inbox, temp_dir, name=name, poll_interval=poll_interval, logger=logger)
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._repo = repository
        self._tagger = tagger
        self._cover_provider = cover_provider
        self._popularity_provider = popularity_provider

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
            self._log.info("[TEMP] %s — Grund: kein AcoustID-Treffer, verschoben zur Prüfung", proc_path.name)
            self._move_to_temp(proc_path)
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

        # Compute target .mp3 path (no album yet — added by register_and_enrich later)
        target_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
        )
        # Score-based overwrite: discard if existing file has a better score
        if target_path.exists():
            existing_score = read_acoustid_score(target_path)
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
                target_path,
                result.score,
                existing_score or 0.0,
            )
            target_path.unlink(missing_ok=True)
            remove_empty_parents(target_path, self._settings.destination)

        # Move .processing → .untested in destination (register_and_enrich handles the rest)
        untested = target_path.with_name(target_path.stem + ".untested" + target_path.suffix)
        untested.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc_path.rename(untested)
        except OSError:
            self._log.error("[DELETE] %s — Grund: Verschieben nach %s fehlgeschlagen", proc_path.name, untested)
            self._move_to_temp(proc_path)
            return

        final_path = await register_and_enrich(
            untested,
            track,
            self._name,
            f"uploader/{self._name}",
            self._settings,
            self._repo,
            self._tagger,
            metadata_provider=self._metadata,
            logger=self._log,
        )
        if final_path is None:
            return

        await fingerprint_song(
            final_path,
            track,
            self._name,
            f"uploader/{self._name}",
            self._settings,
            self._fingerprint,
            self._repo,
            self._tagger,
            cover_provider=self._cover_provider,
            popularity_provider=self._popularity_provider,
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
            self._log.warning("[%s] Lyrics fetch failed for %s", self._name, final_path.name)


__all__ = ["Uploader"]
