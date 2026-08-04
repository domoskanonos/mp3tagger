"""Dateisystem-Event-Quelle auf Basis von ``watchdog`` (inotify) — ohne Polling.

Der :class:`FsEventSource` registriert ``watchdog``-Observer auf Verzeichnissen
und leitet Events thread-sicher (via ``loop.call_soon_threadsafe``) in die
asyncio-Event-Schleife um. Es findet **kein periodischer Zugriff** auf die
Dateisysteme statt — eine schlafende Festplatte wird nie unnötig aufgeweckt.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

_LOGGER = logging.getLogger("radio_ripper.fs_events")


class _Handler(FileSystemEventHandler):
    """Leitet relevante Dateisystem-Events an die :class:`FsEventSource` weiter."""

    def __init__(self, source: FsEventSource, predicate: Callable[[str], bool] | None) -> None:
        self._source = source
        self._predicate = predicate

    def _matches(self, *paths: object | None) -> bool:
        if self._predicate is None:
            return True
        return any(p is not None and self._predicate(str(p)) for p in paths)

    def on_created(self, event: FileSystemEvent) -> None:
        if self._matches(event.src_path):
            self._source._notify()

    def on_modified(self, event: FileSystemEvent) -> None:
        if self._matches(event.src_path):
            self._source._notify()

    def on_moved(self, event: FileSystemEvent) -> None:
        # Nur den Zielpfad prüfen: ein Move .mp3 → .processing (interne Umbenennung
        # durch den Processor) soll keinen neuen Scan auslösen.
        if self._matches(getattr(event, "dest_path", None)):
            self._source._notify()

    def on_deleted(self, event: FileSystemEvent) -> None:
        if self._matches(event.src_path):
            self._source._notify()


class FsEventSource:
    """Beobachtet Verzeichnisse via inotify und liefert Events als asyncio-Signale."""

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._observer = Observer()
        self._event = asyncio.Event()
        self._stopped = False

    def watch(self, path: Path, *, predicate: Callable[[str], bool] | None = None) -> None:
        """Beobachtet *path*; *predicate* filtert auf den src_path/Dest-Pfad."""
        handler = _Handler(self, predicate)
        self._observer.schedule(handler, str(path), recursive=False)
        _LOGGER.debug("Watching %s (inotify)", path)

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._observer.stop()
        with contextlib.suppress(RuntimeError):
            self._observer.join(timeout=2.0)
        # Wartende Coroutine aufwecken
        self._loop.call_soon_threadsafe(self._event.set)

    def _notify(self) -> None:
        if self._stopped:
            return
        self._loop.call_soon_threadsafe(self._event.set)

    async def wait(self) -> bool:
        """Wartet auf das nächste Event. Gibt ``False`` zurück wenn gestoppt."""
        await self._event.wait()
        self._event.clear()
        return not self._stopped


__all__ = ["FsEventSource"]
