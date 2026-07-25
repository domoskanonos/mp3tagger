"""Track repository — persistence of successfully recorded songs.

The :class:`TrackRepository` ABC abstracts persistence so the SQLite backend can
be swapped (or tested with an in-memory implementation). SQLite implementation
keeps a single shared connection guarded by an :class:`asyncio.Lock` because the
default ``sqlite3`` driver is synchronous and not thread-safe by default.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from radio_ripper.domain.models import SavedTrack
from radio_ripper.infra.errors import RepositoryError


@dataclass(slots=True)
class TrackRecord:
    """A :class:`SavedTrack` together with its originating station name."""

    station_name: str
    track: SavedTrack


_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    station_name    TEXT NOT NULL,
    stream_title    TEXT NOT NULL,
    artist          TEXT,
    title           TEXT,
    album           TEXT,
    year            TEXT,
    file_path       TEXT,
    file_size       INTEGER,
    has_cover       INTEGER DEFAULT 0,
    enrichment      TEXT,
    created_at      TEXT DEFAULT (datetime('now')),
    UNIQUE(station_name, stream_title)
)
"""

_MIGRATION_COLUMNS = (
    ("album", "TEXT"),
    ("year", "TEXT"),
    ("has_cover", "INTEGER DEFAULT 0"),
    ("enrichment", "TEXT"),
    ("acoustid_recording_id", "TEXT"),
    ("acoustid_score", "REAL"),
    ("label", "TEXT"),
    ("track_number", "INTEGER"),
    ("disc_number", "INTEGER"),
)


class TrackRepository(ABC):
    """Persistence port for recorded-track metadata."""

    @abstractmethod
    async def exists(self, station_name: str, stream_title: str) -> bool:
        """Return ``True`` if ``(station_name, stream_title)`` is already stored."""

    @abstractmethod
    async def register(self, track: SavedTrack, station_name: str) -> None:
        """Insert a recorded track (ignored on duplicate key)."""

    @abstractmethod
    async def update_fingerprint(
        self,
        station_name: str,
        stream_title: str,
        *,
        recording_id: str,
        score: float,
    ) -> None:
        """Update a registered track with AcoustID fingerprint results."""

    @abstractmethod
    async def find_all_by_recording_id(self, recording_id: str) -> list[TrackRecord]:
        """Return ALL track records matching *recording_id* (empty list if none)."""

    @abstractmethod
    async def find_all_by_artist_title(self, artist: str, title: str) -> list[TrackRecord]:
        """Return ALL track records matching *artist* and *title* (empty list if none)."""

    @abstractmethod
    async def update_enrichment(
        self,
        station_name: str,
        stream_title: str,
        *,
        artist: str | None = None,
        title: str | None = None,
        album: str | None = None,
        year: str | None = None,
        label: str | None = None,
        track_number: int | None = None,
        disc_number: int | None = None,
        file_size: int | None = None,
        has_cover: bool = False,
        enrichment: str = "",
    ) -> None:
        """Update a previously-registered track with enrichment results."""

    @abstractmethod
    async def find_by_file_path(self, file_path: str) -> TrackRecord | None:
        """Return the record for an exact *file_path*, or ``None``."""

    @abstractmethod
    async def list_all(self) -> list[TrackRecord]:
        """Return every stored record."""

    @abstractmethod
    async def update_file_path(self, station_name: str, stream_title: str, new_path: str) -> None:
        """Update the file path after renaming (e.g. .untested.mp3 → .mp3)."""

    @abstractmethod
    async def remove(self, station_name: str, stream_title: str) -> None:
        """Remove a previously-registered track from the repository."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release resources."""


class SQLiteTrackRepository(TrackRepository):
    """Default SQLite (WAL) backend, async-safe via an :class:`asyncio.Lock`."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._conn.execute(_CREATE_SCHEMA)
        for col, decl in _MIGRATION_COLUMNS:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE songs ADD COLUMN {col} {decl}")

    @staticmethod
    def _run(coro: object) -> object:
        return coro

    async def exists(self, station_name: str, stream_title: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._exists_sync, station_name, stream_title)

    def _exists_sync(self, station_name: str, stream_title: str) -> bool:
        try:
            cur = self._conn.execute(
                "SELECT 1 FROM songs "
                "WHERE LOWER(station_name)=LOWER(?) AND LOWER(stream_title)=LOWER(?) LIMIT 1",
                (station_name, stream_title),
            )
            return cur.fetchone() is not None
        except sqlite3.Error as exc:
            raise RepositoryError(f"exists() failed: {exc}") from exc

    async def register(self, track: SavedTrack, station_name: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._register_sync, track, station_name)

    def _register_sync(self, track: SavedTrack, station_name: str) -> None:
        try:
            self._conn.execute(
                """
                INSERT OR IGNORE INTO songs
                    (station_name, stream_title, artist, title,
                     album, year, file_path, file_size, has_cover, enrichment,
                     acoustid_recording_id, acoustid_score,
                     label, track_number, disc_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    station_name,
                    track.stream_title,
                    track.artist,
                    track.title,
                    track.album,
                    track.year,
                    track.file_path,
                    track.file_size,
                    1 if track.has_cover else 0,
                    track.enrichment,
                    track.acoustid_recording_id,
                    track.acoustid_score,
                    track.label,
                    track.track_number,
                    track.disc_number,
                ),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"register() failed: {exc}") from exc

    async def update_enrichment(
        self,
        station_name: str,
        stream_title: str,
        *,
        artist: str | None = None,
        title: str | None = None,
        album: str | None = None,
        year: str | None = None,
        label: str | None = None,
        track_number: int | None = None,
        disc_number: int | None = None,
        file_size: int | None = None,
        has_cover: bool = False,
        enrichment: str = "",
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_enrichment_sync,
                station_name,
                stream_title,
                artist,
                title,
                album,
                year,
                label,
                track_number,
                disc_number,
                file_size,
                has_cover,
                enrichment,
            )

    def _update_enrichment_sync(
        self,
        station_name: str,
        stream_title: str,
        artist: str | None,
        title: str | None,
        album: str | None,
        year: str | None,
        label: str | None,
        track_number: int | None,
        disc_number: int | None,
        file_size: int | None,
        has_cover: bool,
        enrichment: str,
    ) -> None:
        try:
            self._conn.execute(
                """
                UPDATE songs SET
                    artist = COALESCE(?, artist),
                    title  = COALESCE(?, title),
                    album  = COALESCE(?, album),
                    year   = COALESCE(?, year),
                    label  = COALESCE(?, label),
                    track_number = COALESCE(?, track_number),
                    disc_number  = COALESCE(?, disc_number),
                    file_size = COALESCE(?, file_size),
                    has_cover = ?,
                    enrichment = ?
                WHERE station_name=? AND LOWER(stream_title)=LOWER(?)
                """,
                (
                    artist,
                    title,
                    album,
                    year,
                    label,
                    track_number,
                    disc_number,
                    file_size,
                    1 if has_cover else 0,
                    enrichment,
                    station_name,
                    stream_title,
                ),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"update_enrichment() failed: {exc}") from exc

    async def update_fingerprint(
        self,
        station_name: str,
        stream_title: str,
        *,
        recording_id: str,
        score: float,
    ) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_fingerprint_sync,
                station_name,
                stream_title,
                recording_id,
                score,
            )

    def _update_fingerprint_sync(
        self,
        station_name: str,
        stream_title: str,
        recording_id: str,
        score: float,
    ) -> None:
        try:
            self._conn.execute(
                """
                UPDATE songs SET
                    acoustid_recording_id = ?,
                    acoustid_score = ?
                WHERE station_name=? AND LOWER(stream_title)=LOWER(?)
                """,
                (recording_id, score, station_name, stream_title),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"update_fingerprint() failed: {exc}") from exc

    async def find_all_by_recording_id(self, recording_id: str) -> list[TrackRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._find_all_by_recording_id_sync, recording_id)

    def _find_all_by_recording_id_sync(self, recording_id: str) -> list[TrackRecord]:
        try:
            cur = self._conn.execute(
                """
                SELECT station_name, stream_title, artist, title,
                       file_path, file_size, album, year, has_cover,
                       enrichment, acoustid_recording_id, acoustid_score,
                       label, track_number, disc_number
                FROM songs WHERE acoustid_recording_id=?
                """,
                (recording_id,),
            )
            result: list[TrackRecord] = []
            for row in cur.fetchall():
                result.append(
                    TrackRecord(
                        station_name=row["station_name"],
                        track=SavedTrack(
                            stream_title=row["stream_title"],
                            artist=row["artist"] or "",
                            title=row["title"] or "",
                            file_path=row["file_path"],
                            file_size=row["file_size"] or 0,
                            album=row["album"],
                            year=row["year"],
                            has_cover=bool(row["has_cover"]),
                            enrichment=row["enrichment"],
                            acoustid_recording_id=row["acoustid_recording_id"],
                            acoustid_score=row["acoustid_score"],
                            label=row["label"],
                            track_number=row["track_number"],
                            disc_number=row["disc_number"],
                        ),
                    )
                )
            return result
        except sqlite3.Error as exc:
            raise RepositoryError(f"find_all_by_recording_id() failed: {exc}") from exc

    async def find_all_by_artist_title(self, artist: str, title: str) -> list[TrackRecord]:
        async with self._lock:
            return await asyncio.to_thread(
                self._find_all_by_artist_title_sync,
                artist,
                title,
            )

    def _find_all_by_artist_title_sync(self, artist: str, title: str) -> list[TrackRecord]:
        try:
            cur = self._conn.execute(
                """
                SELECT station_name, stream_title, artist, title,
                       file_path, file_size, album, year, has_cover,
                       enrichment, acoustid_recording_id, acoustid_score,
                       label, track_number, disc_number
                FROM songs
                WHERE LOWER(artist)=LOWER(?) AND LOWER(title)=LOWER(?)
                """,
                (artist, title),
            )
            result: list[TrackRecord] = []
            for row in cur.fetchall():
                result.append(
                    TrackRecord(
                        station_name=row["station_name"],
                        track=SavedTrack(
                            stream_title=row["stream_title"],
                            artist=row["artist"] or "",
                            title=row["title"] or "",
                            file_path=row["file_path"],
                            file_size=row["file_size"] or 0,
                            album=row["album"],
                            year=row["year"],
                            has_cover=bool(row["has_cover"]),
                            enrichment=row["enrichment"],
                            acoustid_recording_id=row["acoustid_recording_id"],
                            acoustid_score=row["acoustid_score"],
                            label=row["label"],
                            track_number=row["track_number"],
                            disc_number=row["disc_number"],
                        ),
                    )
                )
            return result
        except sqlite3.Error as exc:
            raise RepositoryError(f"find_all_by_artist_title() failed: {exc}") from exc

    async def find_by_file_path(self, file_path: str) -> TrackRecord | None:
        async with self._lock:
            return await asyncio.to_thread(self._find_by_file_path_sync, file_path)

    def _find_by_file_path_sync(self, file_path: str) -> TrackRecord | None:
        try:
            cur = self._conn.execute(
                """
                SELECT station_name, stream_title, artist, title,
                       file_path, file_size, album, year, has_cover,
                       enrichment, acoustid_recording_id, acoustid_score,
                       label, track_number, disc_number
                FROM songs WHERE file_path=? LIMIT 1
                """,
                (file_path,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return TrackRecord(
                station_name=row["station_name"],
                track=SavedTrack(
                    stream_title=row["stream_title"],
                    artist=row["artist"] or "",
                    title=row["title"] or "",
                    file_path=row["file_path"],
                    file_size=row["file_size"] or 0,
                    album=row["album"],
                    year=row["year"],
                    has_cover=bool(row["has_cover"]),
                    enrichment=row["enrichment"],
                    acoustid_recording_id=row["acoustid_recording_id"],
                    acoustid_score=row["acoustid_score"],
                    label=row["label"],
                    track_number=row["track_number"],
                    disc_number=row["disc_number"],
                ),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"find_by_file_path() failed: {exc}") from exc

    async def list_all(self) -> list[TrackRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._list_all_sync)

    def _list_all_sync(self) -> list[TrackRecord]:
        try:
            cur = self._conn.execute(
                """
                SELECT station_name, stream_title, artist, title,
                       file_path, file_size, album, year, has_cover,
                       enrichment, acoustid_recording_id, acoustid_score,
                       label, track_number, disc_number
                FROM songs ORDER BY station_name, stream_title
                """
            )
            result: list[TrackRecord] = []
            for row in cur.fetchall():
                result.append(
                    TrackRecord(
                        station_name=row["station_name"],
                        track=SavedTrack(
                            stream_title=row["stream_title"],
                            artist=row["artist"] or "",
                            title=row["title"] or "",
                            file_path=row["file_path"],
                            file_size=row["file_size"] or 0,
                            album=row["album"],
                            year=row["year"],
                            has_cover=bool(row["has_cover"]),
                            enrichment=row["enrichment"],
                            acoustid_recording_id=row["acoustid_recording_id"],
                            acoustid_score=row["acoustid_score"],
                            label=row["label"],
                            track_number=row["track_number"],
                            disc_number=row["disc_number"],
                        ),
                    )
                )
            return result
        except sqlite3.Error as exc:
            raise RepositoryError(f"list_all() failed: {exc}") from exc

    async def update_file_path(self, station_name: str, stream_title: str, new_path: str) -> None:
        async with self._lock:
            await asyncio.to_thread(
                self._update_file_path_sync, station_name, stream_title, new_path
            )

    def _update_file_path_sync(self, station_name: str, stream_title: str, new_path: str) -> None:
        try:
            self._conn.execute(
                "UPDATE songs SET file_path=? WHERE station_name=? AND LOWER(stream_title)=LOWER(?)",
                (new_path, station_name, stream_title),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"update_file_path() failed: {exc}") from exc

    async def remove(self, station_name: str, stream_title: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._remove_sync, station_name, stream_title)

    def _remove_sync(self, station_name: str, stream_title: str) -> None:
        try:
            self._conn.execute(
                "DELETE FROM songs WHERE station_name=? AND stream_title=?",
                (station_name, stream_title),
            )
        except sqlite3.Error as exc:
            raise RepositoryError(f"remove() failed: {exc}") from exc

    async def aclose(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._conn.close)


__all__ = ["SQLiteTrackRepository", "TrackRepository"]
