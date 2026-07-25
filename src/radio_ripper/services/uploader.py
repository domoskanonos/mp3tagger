"""Inbox-based MP3 uploader — thin orchestrator reusing the shared pipeline.

Scans *mp3_inbox* for ``.mp3`` files, fingerprints them to determine
identity, then runs them through the same post-processing pipeline as
live-stream recordings (register, enrich, tag, album-move, CAA cover,
popularity check, cross-station dedup).
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
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import compute_file_path
from radio_ripper.services.tagging import TrackTagger
from radio_ripper.services.track_processing import (
    fingerprint_song,
    register_and_enrich,
)


class Uploader:
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
        cover_provider: Any | None = None,
        popularity_provider: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._repo = repository
        self._tagger = tagger
        self._name = name
        self._poll_interval = poll_interval
        self._cover_provider = cover_provider
        self._popularity_provider = popularity_provider
        self._log = logger or logging.getLogger(__name__)
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

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
            "Uploader started — polling %s every %.0fs",
            self._inbox,
            self._poll_interval,
        )
        while not self._stop_event.is_set():
            try:
                await self._process_inbox()
            except Exception:
                self._log.exception("Uploader inbox scan failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._poll_interval)
        self._log.info("Uploader stopped")

    async def _process_inbox(self) -> None:
        for mp3_path in sorted(self._inbox.glob("*.mp3")):
            if self._stop_event.is_set():
                return
            try:
                await self._process_one(mp3_path)
            except Exception:
                self._log.exception("Unexpected error processing %s", mp3_path)

    async def _process_one(self, mp3_path: Path) -> None:
        proc_path = mp3_path.with_suffix(".processing")
        try:
            mp3_path.rename(proc_path)
        except OSError:
            self._log.warning("Cannot rename %s (concurrent access?) — skipping", mp3_path)
            return

        try:
            await self._fingerprint_and_route(proc_path)
        except Exception:
            self._log.exception("Failed to process %s — moving to temp", proc_path.name)
            self._move_to_temp(proc_path)

    async def _fingerprint_and_route(self, proc_path: Path) -> None:
        try:
            result = await self._fingerprint.fingerprint(proc_path)
        except NonRetriableFingerprintError:
            self._log.warning("Corrupt/unreadable %s — deleting", proc_path.name)
            self._cleanup_file(proc_path)
            return
        except FingerprintError:
            self._log.warning(
                "Fingerprint infrastructure error for %s — moving to temp for retry",
                proc_path.name,
            )
            self._move_to_temp(proc_path)
            return

        if result is None or not result.recording_id:
            self._log.info("No fingerprint match for %s — moving to temp", proc_path.name)
            self._move_to_temp(proc_path)
            return

        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)
        base_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
            overwrite=self._settings.overwrite_existing_files,
        )
        untested = base_path.with_name(base_path.stem + ".untested" + base_path.suffix)
        untested.parent.mkdir(parents=True, exist_ok=True)
        try:
            proc_path.rename(untested)
        except OSError:
            self._log.error("Cannot move %s → %s", proc_path, untested)
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

        # Rename .untested → .mp3, update tags, fetch CAA cover,
        # check popularity, cross-station dedup
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

        # Fetch & write lyrics
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
            self._log.warning("[%s] Lyrics fetch failed for %s", self._name, final_path.name)

    def _move_to_temp(self, path: Path) -> None:
        self._temp_dir.mkdir(parents=True, exist_ok=True)
        # Restore original .mp3 extension (the file was renamed to .processing)
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


__all__ = ["Uploader"]
