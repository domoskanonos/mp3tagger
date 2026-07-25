"""Base inbox processor — shared polling loop for FileProcessor and Uploader."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path


class BaseInboxProcessor(ABC):
    """Poll an *inbox* directory and process ``.mp3`` files as they arrive.

    Subclasses implement :meth:`_process_file` which receives a ``.processing``
    -renamed path and must handle it (fingerprint, enrich, tag, move).
    """

    def __init__(
        self,
        inbox: Path,
        temp_dir: Path,
        *,
        name: str = "processor",
        poll_interval: float = 5.0,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._name = name
        self._poll_interval = poll_interval
        self._log = logger or logging.getLogger(f"radio_ripper.{name}")
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
            self._log.warning("Cannot rename %s (concurrent access?) — skipping", mp3_path)
            return

        try:
            await self._process_file(proc_path)
        except Exception:
            self._log.exception("Failed to process %s", proc_path.name)
            await self._on_processing_error(proc_path)

    async def _on_processing_error(self, proc_path: Path) -> None:
        """Called when :meth:`_process_file` raises an unexpected exception.

        Default: move the file to *temp_dir* for manual inspection.
        Subclasses may override (e.g. to delete instead).
        """
        self._move_to_temp(proc_path)

    @abstractmethod
    async def _process_file(self, proc_path: Path) -> None:
        """Process a single file that has been renamed to ``.processing``."""

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
