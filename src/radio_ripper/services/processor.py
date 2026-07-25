"""File processor — single-worker inbox processing for recorded MP3s.

Scans an ``inbox`` (streaming_results/ or mp3_inbox) for ``.mp3`` files and
processes them one by one: fingerprint → enrich → tag → move to destination.
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
from radio_ripper.services.storage import compute_file_path, remux_mp3
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.track_processing import (
    enrich_and_file,
    fingerprint_song,
)


class FileProcessor(BaseInboxProcessor):
    """Single-worker inbox processor.

    Polls *inbox* for ``.mp3`` files, processes each sequentially:
      1. Fingerprint (AcoustID).
      2. Enrich (iTunes) + basic tags.
      3. Rename ``.untested`` → ``.mp3``, CAA cover, MB metadata,
         artist image, lyrics.
      4. Move to ``destination/`` (album subfolder when available).
    Any failure → file is deleted.
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
            self._log.warning("Corrupt/unreadable %s — deleting", proc_path.name)
            self._cleanup_file(proc_path)
            return
        except FingerprintError:
            self._log.warning(
                "Fingerprint error for %s — moving to temp for inspection",
                proc_path.name,
            )
            self._move_to_temp(proc_path)
            return

        if result is None or not result.recording_id:
            self._log.info("No fingerprint match for %s — deleting", proc_path.name)
            self._cleanup_file(proc_path)
            return

        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)
        base = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
            overwrite=self._settings.overwrite_existing_files,
        )
        untested = base.with_name(base.stem + ".untested" + base.suffix)
        untested.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc_path.rename(untested)
        except OSError:
            self._log.error("Cannot move %s → %s", proc_path, untested)
            self._cleanup_file(proc_path)
            return

        try:
            await self._enrich_and_finalize(untested, track, result)
        except Exception:
            self._log.exception("Processing failed for %s — deleting", untested.name)
            self._cleanup_file(untested)

    async def _on_processing_error(self, proc_path: Path) -> None:
        self._cleanup_file(proc_path)

    async def _enrich_and_finalize(
        self,
        file_path: Path,
        track: TrackInfo,
        result: FingerprintResult,
    ) -> None:
        """Enrich, tag, apply fingerprint, fetch cover/lyrics, move to dest."""
        provenance = f"{self._name}/{self._name}"

        remux_mp3(file_path)

        final_path = await enrich_and_file(
            file_path,
            track,
            self._name,
            provenance,
            self._settings,
            self._tagger,
            metadata_provider=self._metadata,
            logger=self._log,
        )
        if final_path is None:
            raise RuntimeError("enrich_and_file returned None")

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


__all__ = ["FileProcessor"]
