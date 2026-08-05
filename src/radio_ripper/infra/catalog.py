"""SQLite-Katalog — Index über die MP3-Sammlung in ``destination/``.

Der Katalog ist ein *Index*, kein Source-of-Truth. Das Dateisystem bleibt
führend; der Katalog beschleunigt nur Abfragen (``find_by_recording_id``,
``find_least_popular``, ``count``) und ermöglicht die Optimierungs-Logik
(Versionsvergleich, Eviction). Bei Inkonsistenz gewinnt immer das Dateisystem.

Beim Start wird :meth:`Catalog.reconcile_with_filesystem` aufgerufen, um den
Katalog mit dem Dateisystem abzugleichen — verwaiste DB-Einträge werden
entfernt, neue Dateien werden indiziert.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mutagen.id3 import ID3, ID3NoHeaderError
from mutagen.mp3 import MP3

_LOGGER = logging.getLogger("radio_ripper.catalog")


def _log_progress(
    logger: logging.Logger,
    done: int,
    total: int,
    label: str,
    last_pct: int | None = None,
) -> int | None:
    """Loggt einen Fortschrittsbalken (ASCII) nur alle 5 % — sonst wird das Log zu groß.

    Gibt den zuletzt geloggten Prozentwert zurück (für den nächsten Aufruf).
    """
    if total <= 0:
        return last_pct
    pct = done * 100 // total
    if pct % 5 != 0 or pct == last_pct:
        return last_pct
    bar_width = 20
    filled = round(pct * bar_width / 100)
    bar = "#" * filled + "-" * (bar_width - filled)
    logger.info("[%s] %3d%% |%s| %d/%d", label, pct, bar, done, total)
    return pct


# ── Datenmodell ──────────────────────────────────────────────────────────────


@dataclass(slots=True)
class SongRecord:
    """Eine Zeile aus dem Katalog — Abbild einer MP3-Datei in ``destination/``."""

    file_path: str
    recording_id: str | None = None
    isrc: str | None = None
    artist: str = ""
    title: str = ""
    album: str | None = None
    genre: str | None = None
    release_group_type: str | None = None
    station_name: str | None = None
    file_size: int | None = None
    bitrate: int | None = None  # kbps
    sample_rate: int | None = None  # Hz
    duration_ms: int | None = None
    acoustid_score: float | None = None
    popularity_rank: int | None = None
    has_cover: bool = False


@dataclass(slots=True)
class ReconcileReport:
    """Ergebnis eines Reconcile-Laufs."""

    added: int = 0  # in DB neu aufgenommen
    removed: int = 0  # verwaiste DB-Einträge gelöscht
    kept: int = 0  # bereits in DB, unverändert
    duration_s: float = 0.0
    errors: list[str] = field(default_factory=list)


# ── ABC ──────────────────────────────────────────────────────────────────────


class Catalog(ABC):
    """Persistence-Port — Index über die MP3-Sammlung."""

    @abstractmethod
    async def upsert(self, rec: SongRecord) -> None: ...

    @abstractmethod
    async def find_by_recording_id(self, recording_id: str) -> list[SongRecord]: ...

    @abstractmethod
    async def find_duplicate_versions(self) -> list[list[SongRecord]]:
        """Gruppen mit gleicher (recording_id, isrc), >1 Treffer — Optimierungskandidaten."""
        ...

    @abstractmethod
    async def find_least_popular(self, limit: int = 20) -> list[SongRecord]: ...

    @abstractmethod
    async def count(self) -> int: ...

    @abstractmethod
    async def exists_by_path(self, file_path: str) -> bool: ...

    @abstractmethod
    async def remove(self, file_path: str) -> None: ...

    @abstractmethod
    async def list_all(self) -> list[SongRecord]: ...

    @abstractmethod
    async def find_missing_tags(self) -> list[SongRecord]:
        """Einträge, bei denen Album, Genre oder Cover fehlt — Enrich-Kandidaten."""
        ...

    @abstractmethod
    async def reconcile_with_filesystem(self, destination: Path, *, concurrency: int = 10) -> ReconcileReport: ...

    @abstractmethod
    async def aclose(self) -> None: ...


# ── SQLite-Implementation ───────────────────────────────────────────────────


_CREATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS songs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    recording_id        TEXT,
    isrc                TEXT,
    artist              TEXT NOT NULL DEFAULT '',
    title               TEXT NOT NULL DEFAULT '',
    album               TEXT,
    genre               TEXT,
    release_group_type  TEXT,
    station_name        TEXT,
    file_path           TEXT NOT NULL UNIQUE,
    file_size           INTEGER,
    bitrate             INTEGER,
    sample_rate         INTEGER,
    duration_ms         INTEGER,
    acoustid_score      REAL,
    popularity_rank     INTEGER,
    has_cover           INTEGER DEFAULT 0,
    created_at          TEXT DEFAULT (datetime('now')),
    updated_at          TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_recording_id ON songs(recording_id);
CREATE INDEX IF NOT EXISTS idx_isrc        ON songs(isrc);
CREATE INDEX IF NOT EXISTS idx_popularity  ON songs(popularity_rank);
CREATE INDEX IF NOT EXISTS idx_artist_title ON songs(artist, title);
"""


_MIGRATION_COLUMNS = (
    ("isrc", "TEXT"),
    ("release_group_type", "TEXT"),
    ("popularity_rank", "INTEGER"),
    ("bitrate", "INTEGER"),
    ("sample_rate", "INTEGER"),
    ("duration_ms", "INTEGER"),
    ("genre", "TEXT"),
)


def _row_to_record(row: sqlite3.Row) -> SongRecord:
    return SongRecord(
        file_path=row["file_path"],
        recording_id=row["recording_id"],
        isrc=row["isrc"],
        artist=row["artist"],
        title=row["title"],
        album=row["album"],
        genre=row["genre"],
        release_group_type=row["release_group_type"],
        station_name=row["station_name"],
        file_size=row["file_size"],
        bitrate=row["bitrate"],
        sample_rate=row["sample_rate"],
        duration_ms=row["duration_ms"],
        acoustid_score=row["acoustid_score"],
        popularity_rank=row["popularity_rank"],
        has_cover=bool(row["has_cover"]),
    )


# ── ID3- und Audio-Lese-Helfer ──────────────────────────────────────────────


def _read_txxx(tags: ID3, desc: str) -> str | None:
    for frame in tags.getall("TXXX"):
        if frame.desc == desc and frame.text:
            return str(frame.text[0])
    return None


def read_tags_from_file(path: Path) -> dict[str, Any]:
    """Liest ID3-Tags (TSRC, TXXX:*, TPE1, TIT2, TALB) aus *path*.

    Liefert ein dict mit den Schlüsseln ``recording_id``, ``isrc``,
    ``release_group_type``, ``artist``, ``title``, ``album``,
    ``acoustid_score``, ``has_cover``.  Bei fehlendem Tag ist der Wert
    ``None`` (bzw. ``False`` für ``has_cover``).
    """
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        return {
            "recording_id": None,
            "isrc": None,
            "release_group_type": None,
            "artist": None,
            "title": None,
            "album": None,
            "genre": None,
            "acoustid_score": None,
            "has_cover": False,
        }
    except Exception:
        return {
            "recording_id": None,
            "isrc": None,
            "release_group_type": None,
            "artist": None,
            "title": None,
            "album": None,
            "genre": None,
            "acoustid_score": None,
            "has_cover": False,
        }

    artist = title = album = genre = None
    if "TPE1" in tags and tags["TPE1"].text:
        artist = str(tags["TPE1"].text[0])
    if "TIT2" in tags and tags["TIT2"].text:
        title = str(tags["TIT2"].text[0])
    if "TALB" in tags and tags["TALB"].text:
        album = str(tags["TALB"].text[0])
    if "TCON" in tags and tags["TCON"].text:
        genre = str(tags["TCON"].text[0])

    isrc = None
    if "TSRC" in tags and tags["TSRC"].text:
        isrc = str(tags["TSRC"].text[0])

    recording_id = _read_txxx(tags, "MusicBrainz Recording Id")
    release_group_type = _read_txxx(tags, "MusicBrainz Release Group Type")
    acoustid_score_raw = _read_txxx(tags, "AcoustID Score")
    acoustid_score: float | None = None
    if acoustid_score_raw is not None:
        with contextlib.suppress(ValueError, TypeError):
            acoustid_score = float(acoustid_score_raw)

    popularity_raw = _read_txxx(tags, "Deezer Popularity Rank")
    popularity_rank: int | None = None
    if popularity_raw is not None:
        with contextlib.suppress(ValueError, TypeError):
            popularity_rank = int(popularity_raw)

    has_cover = any(frame.__class__.__name__ == "APIC" for frame in tags.getall("APIC"))

    return {
        "recording_id": recording_id,
        "isrc": isrc,
        "release_group_type": release_group_type,
        "artist": artist,
        "title": title,
        "album": album,
        "genre": genre,
        "acoustid_score": acoustid_score,
        "popularity_rank": popularity_rank,
        "has_cover": has_cover,
    }


def read_audio_from_file(path: Path) -> dict[str, int | None]:
    """Liest ``bitrate`` (kbps), ``sample_rate`` (Hz), ``duration_ms`` via mutagen.

    Liefert ein dict; bei Lesefehlern sind alle Werte ``None``.
    """
    try:
        info = MP3(path).info
        if info is None:
            return {"bitrate": None, "sample_rate": None, "duration_ms": None}
    except Exception:
        return {"bitrate": None, "sample_rate": None, "duration_ms": None}
    return {
        "bitrate": info.bitrate // 1000 if info.bitrate else None,
        "sample_rate": info.sample_rate if info.sample_rate else None,
        "duration_ms": int(info.length * 1000) if info.length else None,
    }


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


class SqliteCatalog(Catalog):
    """Standard SQLite-Backend (WAL), async-safe via :class:`asyncio.Lock`."""

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
        self._conn.executescript(_CREATE_SCHEMA)
        for col, decl in _MIGRATION_COLUMNS:
            with contextlib.suppress(sqlite3.OperationalError):
                self._conn.execute(f"ALTER TABLE songs ADD COLUMN {col} {decl}")

    # ── upsert ──────────────────────────────────────────────────────────────

    async def upsert(self, rec: SongRecord) -> None:
        async with self._lock:
            await asyncio.to_thread(self._upsert_sync, rec)

    def _upsert_sync(self, rec: SongRecord) -> None:
        self._conn.execute(
            """
            INSERT INTO songs (
                recording_id, isrc, artist, title, album, genre,
                release_group_type, station_name, file_path, file_size,
                bitrate, sample_rate, duration_ms,
                acoustid_score, popularity_rank, has_cover, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(file_path) DO UPDATE SET
                recording_id=excluded.recording_id,
                isrc=excluded.isrc,
                artist=excluded.artist,
                title=excluded.title,
                album=excluded.album,
                genre=excluded.genre,
                release_group_type=excluded.release_group_type,
                station_name=excluded.station_name,
                file_size=excluded.file_size,
                bitrate=excluded.bitrate,
                sample_rate=excluded.sample_rate,
                duration_ms=excluded.duration_ms,
                acoustid_score=excluded.acoustid_score,
                popularity_rank=excluded.popularity_rank,
                has_cover=excluded.has_cover,
                updated_at=datetime('now')
            """,
            (
                rec.recording_id,
                rec.isrc,
                rec.artist,
                rec.title,
                rec.album,
                rec.genre,
                rec.release_group_type,
                rec.station_name,
                rec.file_path,
                rec.file_size,
                rec.bitrate,
                rec.sample_rate,
                rec.duration_ms,
                rec.acoustid_score,
                rec.popularity_rank,
                1 if rec.has_cover else 0,
            ),
        )

    # ── find ────────────────────────────────────────────────────────────────

    async def find_by_recording_id(self, recording_id: str) -> list[SongRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._find_by_recording_id_sync, recording_id)

    def _find_by_recording_id_sync(self, recording_id: str) -> list[SongRecord]:
        cur = self._conn.execute(
            "SELECT * FROM songs WHERE recording_id=? ORDER BY acoustid_score DESC NULLS LAST",
            (recording_id,),
        )
        return [_row_to_record(r) for r in cur.fetchall()]

    async def find_duplicate_versions(self) -> list[list[SongRecord]]:
        """Gruppen mit gleicher (recording_id, isrc) und >1 Treffer.

        ``recording_id`` und ``isrc`` müssen beide non-null sein.
        """
        async with self._lock:
            return await asyncio.to_thread(self._find_duplicate_versions_sync)

    def _find_duplicate_versions_sync(self) -> list[list[SongRecord]]:
        cur = self._conn.execute(
            """
            SELECT * FROM songs
            WHERE recording_id IS NOT NULL AND isrc IS NOT NULL
            AND recording_id IN (
                SELECT recording_id FROM songs
                WHERE recording_id IS NOT NULL AND isrc IS NOT NULL
                GROUP BY recording_id, isrc
                HAVING COUNT(*) > 1
            )
            ORDER BY recording_id, isrc, acoustid_score DESC NULLS LAST
            """
        )
        rows = [_row_to_record(r) for r in cur.fetchall()]
        groups: dict[tuple[str, str], list[SongRecord]] = {}
        for r in rows:
            key = (r.recording_id or "", r.isrc or "")
            groups.setdefault(key, []).append(r)
        return [g for g in groups.values() if len(g) > 1]

    async def find_least_popular(self, limit: int = 20) -> list[SongRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._find_least_popular_sync, limit)

    def _find_least_popular_sync(self, limit: int) -> list[SongRecord]:
        cur = self._conn.execute(
            "SELECT * FROM songs WHERE popularity_rank IS NOT NULL ORDER BY popularity_rank ASC LIMIT ?",
            (limit,),
        )
        return [_row_to_record(r) for r in cur.fetchall()]

    async def count(self) -> int:
        async with self._lock:
            return await asyncio.to_thread(self._count_sync)

    def _count_sync(self) -> int:
        cur = self._conn.execute("SELECT COUNT(*) FROM songs")
        return int(cur.fetchone()[0])

    async def exists_by_path(self, file_path: str) -> bool:
        async with self._lock:
            return await asyncio.to_thread(self._exists_by_path_sync, file_path)

    def _exists_by_path_sync(self, file_path: str) -> bool:
        cur = self._conn.execute("SELECT 1 FROM songs WHERE file_path=? LIMIT 1", (file_path,))
        return cur.fetchone() is not None

    async def remove(self, file_path: str) -> None:
        async with self._lock:
            await asyncio.to_thread(self._remove_sync, file_path)

    def _remove_sync(self, file_path: str) -> None:
        self._conn.execute("DELETE FROM songs WHERE file_path=?", (file_path,))

    async def list_all(self) -> list[SongRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._list_all_sync)

    def _list_all_sync(self) -> list[SongRecord]:
        cur = self._conn.execute("SELECT * FROM songs")
        return [_row_to_record(r) for r in cur.fetchall()]

    async def find_missing_tags(self) -> list[SongRecord]:
        async with self._lock:
            return await asyncio.to_thread(self._find_missing_tags_sync)

    def _find_missing_tags_sync(self) -> list[SongRecord]:
        cur = self._conn.execute(
            """
            SELECT * FROM songs
            WHERE (album IS NULL OR album = '')
               OR (genre IS NULL OR genre = '')
               OR has_cover = 0
            """
        )
        return [_row_to_record(r) for r in cur.fetchall()]

    # ── reconcile ──────────────────────────────────────────────────────────

    async def reconcile_with_filesystem(self, destination: Path, *, concurrency: int = 10) -> ReconcileReport:
        """Synchronisiert DB ⇄ Dateisystem einmalig.

        1. Alle DB-Zeilen laden:
           - Datei fehlt auf Disk → DB-Eintrag löschen (Dateileiche).
           - Datei existiert → Tags neu einlesen und DB-Eintrag aktualisieren.
        2. ``destination/`` rekursiv scannen; neue MP3s (noch nicht in DB) indizieren.
        """
        import time

        start = time.monotonic()
        report = ReconcileReport()

        sem = asyncio.Semaphore(concurrency)

        # Schritt 1: vorhandene Einträge prüfen — verwaiste löschen, existierende aktualisieren
        all_rows = await self.list_all()
        known_paths: set[str] = set()

        step1_total = len(all_rows)
        done = 0
        last_pct = _log_progress(_LOGGER, 0, step1_total, "Reconcile (Einträge)")

        def _tick1() -> None:
            nonlocal done, last_pct
            done += 1
            last_pct = _log_progress(_LOGGER, done, step1_total, "Reconcile (Einträge)", last_pct)

        async def _refresh_existing(row: SongRecord) -> None:
            async with sem:
                try:
                    if not Path(row.file_path).exists():
                        await self.remove(row.file_path)
                        report.removed += 1
                        _LOGGER.debug("[Reconcile] entfernt verwaisten DB-Eintrag: %s", row.file_path)
                        return
                    # Datei existiert → Tags neu lesen und DB aktualisieren
                    known_paths.add(row.file_path)
                    try:
                        rec = await asyncio.to_thread(self._build_record_from_file, Path(row.file_path))
                        if rec is not None:
                            # station_name aus vorhandenem DB-Eintrag erhalten (steht nicht im Tag)
                            refreshed = SongRecord(
                                file_path=rec.file_path,
                                recording_id=rec.recording_id,
                                isrc=rec.isrc,
                                artist=rec.artist,
                                title=rec.title,
                                album=rec.album,
                                release_group_type=rec.release_group_type,
                                station_name=row.station_name,
                                file_size=rec.file_size,
                                bitrate=rec.bitrate,
                                sample_rate=rec.sample_rate,
                                duration_ms=rec.duration_ms,
                                acoustid_score=rec.acoustid_score,
                                popularity_rank=rec.popularity_rank,
                                has_cover=rec.has_cover,
                            )
                            await self.upsert(refreshed)
                            report.kept += 1
                    except Exception as exc:
                        report.errors.append(f"{row.file_path}: {exc}")
                        _LOGGER.debug("[Reconcile] Fehler beim Aktualisieren von %s: %s", row.file_path, exc)
                        report.kept += 1  # zählen trotzdem als vorhanden
                finally:
                    _tick1()

        await asyncio.gather(*(_refresh_existing(row) for row in all_rows))

        # Schritt 2: neue Dateien indizieren
        mp3_files: list[Path] = [p for p in destination.rglob("*.mp3") if str(p) not in known_paths]

        step2_total = len(mp3_files)
        done2 = 0
        last_pct2 = _log_progress(_LOGGER, 0, step2_total, "Reconcile (Neue)")

        def _tick2() -> None:
            nonlocal done2, last_pct2
            done2 += 1
            last_pct2 = _log_progress(_LOGGER, done2, step2_total, "Reconcile (Neue)", last_pct2)

        async def _index_one(path: Path) -> None:
            async with sem:
                try:
                    rec = await asyncio.to_thread(self._build_record_from_file, path)
                    if rec is not None:
                        await self.upsert(rec)
                        report.added += 1
                        _LOGGER.debug("[Reconcile] indiziert: %s", path)
                except Exception as exc:
                    report.errors.append(f"{path}: {exc}")
                    _LOGGER.debug("[Reconcile] Fehler bei %s: %s", path, exc)
                finally:
                    _tick2()

        if mp3_files:
            await asyncio.gather(*(_index_one(p) for p in mp3_files))

        report.duration_s = time.monotonic() - start
        _LOGGER.info(
            "[Reconcile] %d added, %d removed, %d kept (gesamt: %d, dauer: %.1fs, fehler: %d)",
            report.added,
            report.removed,
            report.kept,
            report.added + report.kept,
            report.duration_s,
            len(report.errors),
        )
        return report

    @staticmethod
    def _build_record_from_file(path: Path) -> SongRecord | None:
        """Liest Tags + Audio-Eigenschaften und baut ein :class:`SongRecord`."""
        tags = read_tags_from_file(path)
        audio = read_audio_from_file(path)
        return SongRecord(
            file_path=str(path),
            recording_id=tags["recording_id"],
            isrc=tags["isrc"],
            artist=tags["artist"] or "",
            title=tags["title"] or "",
            album=tags["album"],
            genre=tags["genre"],
            release_group_type=tags["release_group_type"],
            station_name=None,
            file_size=_safe_size(path),
            bitrate=audio["bitrate"],
            sample_rate=audio["sample_rate"],
            duration_ms=audio["duration_ms"],
            acoustid_score=tags["acoustid_score"],
            popularity_rank=tags["popularity_rank"],
            has_cover=tags["has_cover"],
        )

    # ── close ──────────────────────────────────────────────────────────────

    async def aclose(self) -> None:
        async with self._lock:
            await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        with contextlib.suppress(sqlite3.Error):
            self._conn.close()


__all__ = [
    "Catalog",
    "ReconcileReport",
    "SongRecord",
    "SqliteCatalog",
    "read_audio_from_file",
    "read_tags_from_file",
]
