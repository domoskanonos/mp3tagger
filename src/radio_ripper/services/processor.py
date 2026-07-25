"""File processor — single-worker inbox processing for recorded MP3s.

Scans an ``inbox`` (streaming_results/ or mp3_inbox) for ``.mp3`` files and
processes them one by one: fingerprint → enrich → tag → move to destination.
No database involved — files are either perfect (in destination/) or deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.storage import compute_file_path
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.track_processing import (
    enrich_and_file,
    fingerprint_song,
)

_LOGGER = logging.getLogger(__name__)


class _NullRepo:
    """Minimal repository stub — no-op for all calls."""

    async def remove(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def find_all_by_artist_title(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def find_all_by_recording_id(self, *args: Any, **kwargs: Any) -> list[Any]:
        return []

    async def update_file_path(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def update_fingerprint(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def aclose(self) -> None:
        pass

class FileProcessor:
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
        cover_provider: Any | None = None,
        popularity_provider: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._tagger = tagger
        self._name = name
        self._poll_interval = poll_interval
        self._cover_provider = cover_provider
        self._popularity = popularity_provider
        self._log = logger or _LOGGER
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._null_repo = _NullRepo()

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

    async def _run(self) -> None:
        self._log.info(
            "Processor started — polling %s every %.0fs",
            self._inbox,
            self._poll_interval,
        )
        while not self._stop_event.is_set():
            try:
                await self._drain_inbox()
            except Exception:
                self._log.exception("Processor inbox scan failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self._poll_interval
                )
        self._log.info("Processor stopped")

    async def _drain_inbox(self) -> None:
        for mp3 in sorted(self._inbox.glob("*.mp3")):
            if self._stop_event.is_set():
                return
            try:
                await self._process_one(mp3)
            except Exception:
                self._log.exception("Unexpected error processing %s", mp3)

    async def _process_one(self, mp3_path: Path) -> None:
        proc_path = mp3_path.with_suffix(".processing")
        try:
            mp3_path.rename(proc_path)
        except OSError:
            self._log.warning(
                "Cannot rename %s (concurrent access?) — skipping", mp3_path
            )
            return

        try:
            await self._fingerprint_and_process(proc_path)
        except Exception:
            self._log.exception("Failed to process %s — deleting", proc_path.name)
            self._cleanup_file(proc_path)

    async def _fingerprint_and_process(self, proc_path: Path) -> None:
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

    async def _enrich_and_finalize(
        self,
        file_path: Path,
        track: TrackInfo,
        result: Any,
    ) -> None:
        """Enrich, tag, apply fingerprint, fetch cover/lyrics, move to dest."""
        provenance = f"{self._name}/{self._name}"

        # Fix MP3 frame alignment from ICY stream cut-points
        from radio_ripper.services.storage import remux_mp3
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
            self._null_repo,  # type: ignore[arg-type]
            self._tagger,
            cover_provider=self._cover_provider,
            popularity_provider=self._popularity,
            logger=self._log,
            precomputed_result=result,
        )

        try:
            from radio_ripper.infra.http import HttpxAsyncClient
            from radio_ripper.services.lyrics import LyricsOvhProvider

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
            self._log.debug(
                "[%s] Lyrics fetch failed for %s", self._name, final_path.name
            )

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
        try:
            path.unlink(missing_ok=True)
        except OSError:
            self._log.exception("Cannot remove %s", path)


__all__ = ["FileProcessor"]
