"""File processor — concurrent inbox processing for recorded MP3s.

Scans an ``inbox`` (source) for ``.mp3`` files and processes up to
``max_concurrent`` of them in parallel. Per file:
fingerprint → [CAA+MB || iTunes+Lyrics+ArtImg || Deezer] (parallel) →
MB-Korrektur → Score-Vergleich → Popularitäts-Prüfung → ONE tag write,
all in a ``work_dir`` staging area, then atomically moved to ``destination/``.
No database involved — files are either perfect (in destination/) or deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import EnrichedInfo, FingerprintResult, MusicBrainzData, TrackInfo
from radio_ripper.infra.catalog import Catalog, SongRecord, read_audio_from_file, read_tags_from_file
from radio_ripper.infra.config import Settings
from radio_ripper.infra.fs_events import FsEventSource
from radio_ripper.services.collection_manager import (
    is_better_version,
    pick_eviction_candidate,
)
from radio_ripper.services.file_utils import compute_file_path, safe_unlink
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.lyrics import LyricsProvider
from radio_ripper.services.metadata_deezer import DeezerData, DeezerMetadataProvider
from radio_ripper.services.metadata_itunes import MetadataProvider
from radio_ripper.services.metadata_musicbrainz import CoverArtProvider
from radio_ripper.services.popularity import PopularityProvider
from radio_ripper.services.tagging import TrackTagger

_TAG_WRITE_MAX_ATTEMPTS = 3
_TAG_WRITE_BASE_DELAY = 0.5

# ── helpers ──


@dataclass(slots=True)
class CollectedMetadata:
    """Gemeinsames Ergebnis der Anreicherung (Phase 3+4) — für neue und Bestandsdateien."""

    result: FingerprintResult
    track: TrackInfo
    enriched: EnrichedInfo | None
    mb_data: MusicBrainzData | None
    deezer_data: DeezerData | None
    deezer_cover: bytes | None
    cover_from_caa: bytes | None
    cover_from_enrich: bytes | None
    deezer_attempted: bool
    artist_image: bytes | None
    lyrics: str | None


def _strip_untested_suffix(
    file_path: Path,
    logger: logging.Logger,
    station_name: str,
    *,
    on_fail: str | None = None,
) -> Path | None:
    """Entfernt den ``.untested``-Suffix aus einer Datei.

    AcoustID hängt ``.untested`` an Dateien an, die noch nicht gematcht wurden.
    Dieses benennt sie zurück zu ``.mp3``, sobald ein Match vorliegt.
    """
    new_path = file_path.with_name(file_path.stem.replace(".untested", "") + ".mp3")
    if file_path == new_path:
        return file_path
    if new_path.exists():
        logger.warning(
            "[%s] Refuse to rename %s -> %s (target exists).%s",
            station_name,
            file_path.name,
            new_path.name,
            on_fail or " Keeping .untested.mp3 for manual review.",
        )
        return None
    try:
        file_path.rename(new_path)
        return new_path
    except OSError as exc:
        logger.warning(
            "[%s] rename %s -> %s failed: %s",
            station_name,
            file_path.name,
            new_path.name,
            exc,
        )
        return None


def correct_fingerprint_result(
    result: FingerprintResult,
    mb_data: MusicBrainzData | None,
    enriched: EnrichedInfo | None = None,
) -> FingerprintResult:
    """Überschreibt AcoustID-Artist/Title mit MusicBrainz-Daten, wenn vorhanden.

    AcoustID liefert oft nur grobe Metadaten oder hat Künstler/Titel
    vertauscht. MusicBrainz ist die kanonische Quelle — wenn MB-Daten
    existieren, werden sie immer bevorzugt. Fallback: Wenn MB keine
    Korrektur liefert (API down/keine Daten), wird der iTunes-Künstler/-Titel
    verwendet, falls er vom AcoustID-Ergebnis abweicht.
    """
    if mb_data and mb_data.recording_artist:
        return FingerprintResult(
            artist=mb_data.recording_artist,
            title=mb_data.recording_title or result.title,
            score=result.score,
            recording_id=result.recording_id,
            artist_mbid=result.artist_mbid,
        )
    if (
        enriched
        and enriched.artist
        and enriched.title
        and (enriched.artist != result.artist or enriched.title != result.title)
    ):
        return FingerprintResult(
            artist=enriched.artist,
            title=enriched.title,
            score=result.score,
            recording_id=result.recording_id,
            artist_mbid=result.artist_mbid,
        )
    return result


async def _fetch_artist_image(
    provider: PopularityProvider,
    artist: str,
    station_name: str,
    logger: logging.Logger,
) -> bytes | None:
    try:
        img = await provider.fetch_artist_image(artist)
        if img is not None:
            return img
    except Exception as exc:
        logger.debug("[%s] artist image fetch failed: %s", station_name, exc)
    return None


async def _fetch_cover_data(
    cover_provider: CoverArtProvider,
    recording_id: str,
    popularity_provider: PopularityProvider | None,
    artist: str,
    station_name: str,
    logger: logging.Logger,
) -> tuple[bytes | None, MusicBrainzData | None, bytes | None]:
    """Holt Cover (CAA), MB-Metadaten und Künstlerbild parallel ab.

    Alle drei API-Aufrufe sind unabhängig und laufen gleichzeitig.
    Fehler werden einzeln abgefangen — ein fehlschlagender Aufruf
    blockiert die anderen nicht.
    """

    async def _fetch_cover() -> bytes | None:
        try:
            return await cover_provider.fetch_cover_by_recording_id(recording_id)
        except Exception as exc:
            logger.debug("[%s] CAA cover lookup failed: %s", station_name, exc)
            return None

    async def _fetch_mb() -> MusicBrainzData | None:
        try:
            d = await cover_provider.fetch_recording_data(recording_id)
            if d and d.release_label:
                logger.info("[%s] MB label: %s", station_name, d.release_label)
            return d
        except Exception as exc:
            logger.debug("[%s] MB recording data lookup failed: %s", station_name, exc)
            return None

    tasks = [_fetch_cover(), _fetch_mb()]
    if popularity_provider is not None and artist:
        tasks.append(_fetch_artist_image(popularity_provider, artist, station_name, logger))
    raw: list[Any] = await asyncio.gather(*tasks, return_exceptions=True)
    cover_b: bytes | None = None if isinstance(raw[0], BaseException) else raw[0]
    mb: MusicBrainzData | None = None if isinstance(raw[1], BaseException) else raw[1]
    art: bytes | None = None
    if len(raw) > 2 and not isinstance(raw[2], BaseException):
        art = raw[2]
    return cover_b, mb, art


async def _fetch_lyrics(
    provider: LyricsProvider,
    artist: str,
    title: str,
    logger: logging.Logger,
    name: str,
) -> str | None:
    try:
        return await provider.fetch(artist, title)
    except Exception as exc:
        logger.debug("[%s] Lyrics fetch failed: %s", name, exc)
        return None


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


# ── processor ──


class FileProcessor:
    """Concurrent inbox processor (Semaphore-begrenzt, ``max_concurrent``).

    Reagiert via inotify auf neue ``.mp3``-Dateien im *inbox* (kein Polling —
    eine schlafende Festplatte wird nicht periodisch aufgeweckt). Pro Datei:

      1. Move ``.processing`` → ``work_dir/``.
      2. Fingerprint (AcoustID) — Score < min → löschen.
      3. Parallel: CAA+MB || iTunes+Lyrics+ArtImg || Deezer.
      4. MB-Korrektur (artist/title) — ggf. Deezer re-fetch.
      5. Score-Vergleich mit bestehender Datei im Destination.
      6. ``.untested`` → ``.mp3`` umbenennen.
      7. Popularitäts-Prüfung über den Deezer-Rang (gleicher API-Call
         wie Phase 3, kein extra Request). Datei wird gelöscht wenn
         Deezer sie nicht kennt oder der Rang unter ``min_popularity_rank``
         liegt.
      8. One-Pass Tag-Write (Cover-Prio: Deezer → CAA → iTunes).
      9. Atomarer Move nach ``destination/``; alte Datei wird bei
         höherem Score ersetzt.

    Wenn ein Schritt scheitert, wird die Stage-Datei gelöscht;
    ``destination/`` wird erst nach vollständigem Erfolg berührt.
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
        cover_provider: CoverArtProvider | None = None,
        deezer_provider: DeezerMetadataProvider | None = None,
        popularity_provider: PopularityProvider | None = None,
        lyrics_provider: LyricsProvider | None = None,
        catalog: Catalog | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._name = name
        self._log = logger or logging.getLogger(f"radio_ripper.{name}")
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._tagger = tagger
        self._cover_provider = cover_provider
        self._deezer_provider = deezer_provider
        self._popularity = popularity_provider
        self._lyrics_provider = lyrics_provider
        self._catalog = catalog
        self._semaphore = asyncio.Semaphore(settings.max_concurrent)
        # Pro recording_id ein Lock — verhindert Race Condition bei gleichzeitiger
        # Verarbeitung desselben Songs durch mehrere Worker.
        self._recording_locks: dict[str, asyncio.Lock] = {}

    def reload_settings(self, settings: Settings) -> None:
        self._settings = settings

    # ── public lifecycle ──

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

    # ── Event-getriebener Loop (inotify, kein Polling) ──

    async def _run(self) -> None:
        watcher = FsEventSource()
        watcher.watch(self._inbox, predicate=lambda p: p.endswith(".mp3"))
        watcher.start()
        self._log.info("%s started — watching %s (inotify)", self._name.title(), self._inbox)
        try:
            while not self._stop_event.is_set():
                try:
                    await self._drain_inbox()
                except Exception:
                    self._log.exception("%s inbox scan failed", self._name.title())
                # Auf neue Dateien warten (inotify) — kein periodisches Polling
                if not await watcher.wait():
                    return
                # Debounce: Events während einer kurzen Pause akkumulieren,
                # damit Batch-Writes nur einen Scan auslösen.
                with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                    await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
        finally:
            watcher.stop()
            self._log.info("%s stopped", self._name.title())

    async def _drain_inbox(self) -> None:
        mp3s = sorted(self._inbox.glob("*.mp3"))
        if not mp3s:
            self._log.debug("No MP3s in %s — waiting for new files (inotify)", self._inbox)
            return
        await self._run_staggered(((mp3, {}) for mp3 in mp3s), self._process_one_staggered)

    async def _process_one_staggered(self, mp3: Path, _pre_tags: dict[str, Any] | None = None) -> None:
        """Verarbeitet eine neue Inbox-Datei (unter dem max_concurrent-Puffer)."""
        async with self._semaphore:
            if self._stop_event.is_set():
                return
            try:
                await self._process_one(mp3)
            except Exception:
                self._log.exception("Unexpected error processing %s", mp3)

    async def _run_staggered(
        self,
        items: Iterable[tuple[Path, dict[str, Any]]],
        worker: Callable[[Path, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Startet Dateien im 1-Sekunden-Takt, max_concurrent als Puffer.

        Einheitlich für Inbox (neue MP3s) und Enrich (Bestandsdateien): Eine neue
        Datei wird pro Sekunde gestartet; `max_concurrent` begrenzt nur die Zahl
        der gleichzeitig in Arbeit befindlichen Dateien (Puffer, nicht erzwungen).
        Dadurch bleibt das MusicBrainz-Rate-Limit (1 req/s) sicher eingehalten,
        da jede Datei höchstens einen MB-Call auslöst.
        """
        in_flight: set[asyncio.Task[None]] = set()

        async def _run(item: tuple[Path, dict[str, Any]]) -> None:
            path, pre_tags = item
            await worker(path, pre_tags)

        for item in items:
            # Puffer voll? → warten, bis ein Slot frei ist
            while len(in_flight) >= self._settings.max_concurrent:
                _done, in_flight = await asyncio.wait(in_flight, return_when=asyncio.FIRST_COMPLETED)
            task = asyncio.create_task(_run(item))
            in_flight.add(task)
            await asyncio.sleep(1.0)  # 1 neue Datei pro Sekunde

        if in_flight:
            await asyncio.gather(*in_flight, return_exceptions=True)

    async def enrich_existing_files(self) -> None:
        """Vervollständigt fehlende Tags/Cover in Bestandsdateien (destination/).

        Läuft NACH dem Reconcile und nur für Dateien, bei denen wirklich etwas
        fehlt (Album, Genre, Cover oder recording_id) — gleicher Anreicherungs-
        und Tag-Flow wie bei neuen MP3s. Es wird NIE gelöscht.
        """
        destination = self._settings.destination
        if not destination.exists():
            return

        # Kandidaten aus dem Katalog (schnelle SQL-Query statt 50.000 Datei-Reads).
        # Voraussetzung: der Reconcile hat die DB bereits gefüllt (reconcile_on_startup).
        candidates: dict[Path, dict[str, Any]] = {}
        if self._catalog is not None:
            try:
                missing = await self._catalog.find_missing_tags()
            except Exception:
                self._log.exception("[%s] Katalog-Abfrage fehlgeschlagen", self._name)
                missing = []
            for rec in missing:
                if not rec.file_path:
                    continue
                candidates[Path(rec.file_path)] = {
                    "artist": rec.artist or "",
                    "title": rec.title or "",
                    "album": rec.album,
                    "genre": rec.genre,
                    "has_cover": rec.has_cover,
                    "has_performer": rec.has_performer,
                    "recording_id": rec.recording_id or "",
                    "acoustid_score": rec.acoustid_score,
                }
            self._log.info(
                "[%s] %d Bestandsdateien mit fehlenden Tags gefunden (aus Katalog).",
                self._name,
                len(candidates),
            )
        else:
            # Fallback ohne Katalog: Dateien direkt scannen (Sicherheitsnetz).
            mp3_files = sorted(destination.rglob("*.mp3"))
            if not mp3_files:
                self._log.info("[%s] Keine Bestandsdateien gefunden.", self._name)
                return
            self._log.info("[%s] Durchsuche %d Bestandsdateien nach fehlenden Tags ...", self._name, len(mp3_files))

            async def _scan_one(path: Path) -> dict[str, Any] | None:
                try:
                    tags = await asyncio.to_thread(read_tags_from_file, path)
                    if not tags.get("album") or not tags.get("genre") or not tags.get("has_cover"):
                        return tags
                except Exception:
                    self._log.debug("[%s] Tags nicht lesbar, überspringe: %s", self._name, path)
                return None

            scan_results = await asyncio.gather(*(_scan_one(p) for p in mp3_files), return_exceptions=True)
            candidates = {}
            for p, tags in zip(mp3_files, scan_results, strict=True):
                if isinstance(tags, dict):
                    candidates[p] = tags

        if not candidates:
            self._log.info("[%s] Keine Bestandsdateien mit fehlenden Tags gefunden.", self._name)
            return

        self._log.info(
            "[%s] Vervollständige fehlende Tags für %d Bestandsdateien ...",
            self._name,
            len(candidates),
        )
        done = 0
        last_pct = _log_progress(self._log, 0, len(candidates), self._name)

        async def _enrich_worker(path: Path, pre_tags: dict[str, Any]) -> None:
            nonlocal done, last_pct
            async with self._semaphore:
                try:
                    await self._enrich_existing_file(path, pre_tags=pre_tags)
                except Exception:
                    self._log.exception("[%s] Fehler bei Bestandsdatei %s", self._name, path.name)
                finally:
                    done += 1
                    last_pct = _log_progress(self._log, done, len(candidates), self._name, last_pct)

        await self._run_staggered(candidates.items(), _enrich_worker)
        self._log.info("[%s] Vervollständigung abgeschlossen (%d Dateien).", self._name, len(candidates))

    async def normalize_filenames(self) -> None:
        """Strikte Dateinamen-Normierung: Dateiname muss mit den artist/title-Tags
        übereinstimmen (Tag = Wahrheit). Fehlbenannte Dateien werden umbenannt und
        ggf. in den korrekten Album-Ordner verschoben.

        Rein lokal (kein API-Call) → kein Stagger/Rate-Limit, möglichst schnell.
        Nutzt die bestehende ``_finalize_and_move``-Logik (Duplikat-Handling via
        is_better_version, leere Alt-Ordner-Bereinigung, Katalog-Upsert).
        """
        destination = self._settings.destination
        if not destination.exists():
            return
        mp3_files = sorted(destination.rglob("*.mp3"))
        if not mp3_files:
            self._log.info("[%s] Keine Bestandsdateien gefunden.", self._name)
            return

        self._log.info("[%s] Prüfe Dateinamen von %d Bestandsdateien ...", self._name, len(mp3_files))
        done = 0
        last_pct = _log_progress(self._log, 0, len(mp3_files), self._name)

        async def _normalize_one(path: Path) -> None:
            nonlocal done, last_pct
            try:
                tags = await asyncio.to_thread(read_tags_from_file, path)
            except Exception:
                self._log.debug("[%s] Tags nicht lesbar, überspringe: %s", self._name, path.name)
                return
            finally:
                done += 1
                last_pct = _log_progress(self._log, done, len(mp3_files), self._name, last_pct)

            artist = tags.get("artist") or ""
            title = tags.get("title") or ""
            if not artist or not title:
                return  # nicht umbenennbar

            meta = self._build_metadata_from_tags(tags)
            await self._finalize_and_move(meta=meta, source_path=path, staged_path=path)

        await asyncio.gather(*(_normalize_one(p) for p in mp3_files), return_exceptions=True)
        self._log.info("[%s] Dateinamen-Prüfung abgeschlossen (%d Dateien).", self._name, len(mp3_files))

    async def _enrich_existing_file(self, path: Path, *, pre_tags: dict[str, Any] | None = None) -> None:
        """Reichert eine Bestandsdatei an und schreibt fehlende Tags in-place.

        Nutzt den GLEICHEN Flow wie neue MP3s (_collect_metadata + _write_tags +
        _finalize_and_move): Bei fehlender recording_id wird gefingert, damit die
        AcoustID-Wahrheit (artist/title/recording_id) und damit das richtige Cover
        gefunden werden. Keine Popularitäts-/Löschlogik für Bestand.

        ``pre_tags`` sind bereits gelesene Tags (z.B. aus dem Katalog) — erspart
        ein erneutes ``read_tags_from_file``.
        """
        if pre_tags:
            tags = pre_tags
        else:
            tags = await asyncio.to_thread(read_tags_from_file, path)
        artist = tags.get("artist") or ""
        title = tags.get("title") or ""
        if not artist or not title:
            self._log.info(
                "[%s] Kein Künstler/Titel in %s — kann nicht anreichern, übersprungen",
                self._name,
                path.name,
            )
            return

        result = self._build_result_from_tags(tags)

        # Wenn die Datei keine recording_id hat, fingerabtasten → AcoustID-Wahrheit
        # (korrekte artist/title/recording_id/artist_mbid) ermitteln.
        if not result.recording_id and self._fingerprint is not None:
            try:
                fp = await self._fingerprint.fingerprint(path)
            except Exception:
                self._log.debug("[%s] Fingerprint für Bestandsdatei fehlgeschlagen: %s", self._name, path.name)
                fp = None
            if fp is not None and fp.recording_id:
                self._log.info(
                    "[%s] Bestandsdatei per Fingerprint zugeordnet: %s -> %s - %s (recording_id=%s)",
                    self._name,
                    path.name,
                    fp.artist,
                    fp.title,
                    fp.recording_id,
                )
                result = fp

        track_pre = TrackInfo(
            stream_title=f"{result.artist} - {result.title}",
            artist=result.artist,
            title=result.title,
        )
        meta = await self._collect_metadata(result, track_pre, path)
        provenance = f"{self._name}/{self._name}"

        # Staging-Kopie im work_dir taggen, dann atomar zurück (kein Teilzustand in der Bibliothek)
        stage = self._settings.work_dir / f"enrich-{path.name}"
        stage.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(path), str(stage))
        except OSError:
            self._log.error("[%s] Kopieren nach work_dir fehlgeschlagen: %s", self._name, path.name)
            return

        ok = await self._write_tags(
            stage_path=stage,
            meta=meta,
            provenance=provenance,
            final_path=path,
        )
        if not ok:
            safe_unlink(stage)
            return

        # Konsistent benennen + verschieben (wie neue MP3s), leere Alt-Ordner löschen.
        await self._finalize_and_move(meta=meta, source_path=path, staged_path=stage)

        # Vorher/Nachher-Vergleich der Tags — logge, was sich wirklich geändert hat.
        self._log_changed_tags(path, tags, self._name)

    @staticmethod
    def _build_result_from_tags(tags: dict[str, Any]) -> FingerprintResult:
        """Baut aus den vorhandenen ID3-Tags ein FingerprintResult (Bestandsfall)."""
        recording_id = tags.get("recording_id") or ""
        artist = tags.get("artist") or ""
        title = tags.get("title") or ""
        score = tags.get("acoustid_score")
        return FingerprintResult(
            artist=artist,
            title=title,
            score=float(score) if score is not None else 0.0,
            recording_id=recording_id,
        )

    @staticmethod
    def _build_metadata_from_tags(tags: dict[str, Any]) -> CollectedMetadata:
        """Baut CollectedMetadata aus vorhandenen ID3-Tags (ohne API-Calls).

        Verwendet für rein lokale Flows wie ``normalize_filenames`` — das
        Gegenstück zu ``_collect_metadata`` (das die API-Anreicherung macht).
        """
        result = FileProcessor._build_result_from_tags(tags)
        artist = result.artist
        title = result.title
        enriched = EnrichedInfo(album=tags.get("album") or None)
        track = TrackInfo(
            stream_title=f"{artist} - {title}",
            artist=artist,
            title=title,
        )
        return CollectedMetadata(
            result=result,
            track=track,
            enriched=enriched,
            mb_data=None,
            deezer_data=None,
            deezer_cover=None,
            cover_from_caa=None,
            cover_from_enrich=None,
            deezer_attempted=False,
            artist_image=None,
            lyrics=None,
        )

    async def _finalize_and_move(
        self,
        *,
        meta: CollectedMetadata,
        source_path: Path,
        staged_path: Path,
    ) -> None:
        """Benennt + verschiebt eine getaggte Datei konsistent zur AcoustID-Wahrheit.

        - Zielpfad via ``compute_file_path`` aus den korrigierten artist/title/album.
        - Ziel existiert nicht → verschieben, leere Alt-Ordner bis destination löschen.
        - Ziel existiert, gleicher Song → bessere Version gewinnt (is_better_version),
          sonst bleibt die existierende Datei (die neue wird verworfen).
        - Ziel existiert, anderer Song (Kollision) → neue Datei verwerfen + Error-Log
          mit allen relevanten Infos. Kein Überschreiben der Bibliothek.
        """
        result = meta.result
        track = meta.track
        enriched = meta.enriched
        final_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            track.stream_title,
            album=enriched.album if enriched and enriched.album else None,
        )

        if final_path == source_path:
            if staged_path == final_path:
                # Datei ist bereits korrekt benannt (z.B. normalize_filenames):
                # kein Move nötig, nur Katalog aktualisieren.
                await self._upsert_final(final_path, meta)
                return
            # Gleicher Pfad, aber Staging-Kopie: getaggte Staging-Datei ersetzt
            # die Originaldatei (in-place), dann Katalog aktualisieren.
            try:
                shutil.move(str(staged_path), str(final_path))
            except OSError as exc:
                self._log.error(
                    "[%s] In-place-Ersetzen fehlgeschlagen — Original bleibt unverändert: %s (%s)",
                    self._name,
                    final_path,
                    exc,
                )
                safe_unlink(staged_path)
                return
            self._log.info("[%s] Bestandsdatei aktualisiert: %s", self._name, final_path)
            await self._upsert_final(final_path, meta)
            return

        if not final_path.exists():
            # Ziel frei → verschieben (EXDEV-sicher), leere Alt-Ordner aufräumen.
            final_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.move(str(staged_path), str(final_path))
            except OSError as exc:
                self._log.error(
                    "[%s] Verschieben nach %s fehlgeschlagen: %s",
                    self._name,
                    final_path,
                    exc,
                )
                safe_unlink(staged_path)
                return
            self._log.info("[%s] Umbenannt: %s -> %s", self._name, source_path.name, final_path)
            safe_unlink(source_path, parents_root=self._settings.destination)
            await self._upsert_final(final_path, meta)
            return

        # ── Ziel existiert bereits ──
        existing_tags = await asyncio.to_thread(read_tags_from_file, final_path)
        existing_recording_id = existing_tags.get("recording_id")
        same_song = bool(existing_recording_id) and existing_recording_id == result.recording_id

        if same_song:
            # Gleicher Song → bessere Version gewinnt.
            existing_score = existing_tags.get("acoustid_score")
            existing_audio = await asyncio.to_thread(read_audio_from_file, final_path)
            new_audio = await asyncio.to_thread(read_audio_from_file, staged_path)
            new_wins = is_better_version(
                result.score,
                new_audio["bitrate"],
                new_audio["sample_rate"],
                float(existing_score) if existing_score is not None else None,
                existing_audio["bitrate"],
                existing_audio["sample_rate"],
            )
            if new_wins:
                self._log.info(
                    "[%s] Bessere Version gewinnt: %s ersetzt %s",
                    self._name,
                    source_path.name,
                    final_path.name,
                )
                final_path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.move(str(staged_path), str(final_path))
                except OSError as exc:
                    self._log.error("[%s] Ersetzen fehlgeschlagen: %s", self._name, exc)
                    safe_unlink(staged_path)
                    return
                safe_unlink(source_path, parents_root=self._settings.destination)
                await self._upsert_final(final_path, meta)
            else:
                self._log.info(
                    "[%s] Bestehende Version gleich/besser — neue verworfen: %s",
                    self._name,
                    source_path.name,
                )
                safe_unlink(staged_path)
                safe_unlink(source_path, parents_root=self._settings.destination)
            return

        # Kollision: anderer Song am Ziel → neue Datei verwerfen + Error-Log.
        self._log.error(
            "[COLLISION] %s — Ziel %s gehört einem anderen Song (recording_id=%r vs %r). "
            "Neue Datei verworfen: artist=%r title=%r score=%.4f alt_pfad=%s",
            source_path.name,
            final_path,
            existing_recording_id,
            result.recording_id,
            result.artist,
            result.title,
            result.score,
            source_path,
        )
        safe_unlink(staged_path)
        safe_unlink(source_path, parents_root=self._settings.destination)

    async def _upsert_final(self, final_path: Path, meta: CollectedMetadata) -> None:
        """Schreibt die finalen Datei-Infos in den Katalog (falls vorhanden)."""
        if self._catalog is None:
            return
        has_cover = (meta.cover_from_caa or meta.deezer_cover or meta.cover_from_enrich) is not None
        has_performer = meta.artist_image is not None
        if not has_cover and not has_performer:
            # Kein Cover/Performer im Meta (z.B. normalize_filenames) → aus den
            # tatsächlichen Datei-Tags übernehmen, damit der Katalog korrekt bleibt.
            try:
                tags = await asyncio.to_thread(read_tags_from_file, final_path)
                has_cover = bool(tags.get("has_cover"))
                has_performer = bool(tags.get("has_performer"))
            except Exception:
                pass
        await self._catalog_upsert(
            final_path,
            meta.result,
            meta.mb_data,
            meta.enriched,
            meta.deezer_data,
            has_cover=has_cover,
            has_performer=has_performer,
        )

    def _log_changed_tags(self, path: Path, before: dict[str, Any], station_name: str) -> None:
        """Loggt, welche Tags sich durch die Anreicherung tatsächlich geändert haben."""
        try:
            after = read_tags_from_file(path)
        except Exception:
            self._log.debug("[%s] Tags nach Anreicherung nicht lesbar: %s", station_name, path.name)
            return

        def _norm(value: Any) -> Any:
            if isinstance(value, str):
                value = value.strip()
                return value or None
            return value

        changes: list[str] = []
        for field, label in (
            ("album", "Album"),
            ("genre", "Genre"),
            ("recording_id", "recording_id"),
            ("isrc", "ISRC"),
            ("artist", "Artist"),
            ("title", "Title"),
        ):
            old = _norm(before.get(field))
            new = _norm(after.get(field))
            if old != new:
                changes.append(f"{label}: {old!r} -> {new!r}")

        cover_before = bool(before.get("has_cover"))
        cover_after = bool(after.get("has_cover"))
        if cover_before != cover_after:
            changes.append(
                f"Cover: {'vorhanden' if cover_before else 'fehlt'} -> {'vorhanden' if cover_after else 'fehlt'}"
            )

        if changes:
            self._log.info(
                "[%s] Tags geändert in %s: %s",
                station_name,
                path.name,
                "; ".join(changes),
            )
        else:
            self._log.debug("[%s] Keine Tag-Änderungen in %s", station_name, path.name)

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
            self._cleanup_file(proc_path)

    # ── helpers ──

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
        safe_unlink(path)

    # ── Hauptverarbeitungsschritte ──

    async def _move_to_work_dir(self, proc_path: Path) -> Path | None:
        """Verschiebt die .processing-Datei aus dem Inbox ins work_dir."""
        work_path = self._settings.work_dir / proc_path.name
        try:
            work_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(proc_path), str(work_path))
            return work_path
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben ins work_dir fehlgeschlagen",
                proc_path.name,
            )
            self._cleanup_file(proc_path)
            return None

    async def _fingerprint_and_validate(self, work_path: Path) -> FingerprintResult | None:
        """Fingerprint + Fehlerbehandlung. Gibt None bei Abbruch."""
        try:
            result = await self._fingerprint.fingerprint(work_path)
        except NonRetriableFingerprintError:
            self._log.warning("[DELETE] %s — Grund: Datei korrupt/nicht lesbar", work_path.name)
            self._cleanup_file(work_path)
            return None
        except FingerprintError:
            self._log.warning(
                "[TEMP] %s — Grund: Fingerprint-Infrastrukturfehler, verschoben zur Prüfung",
                work_path.name,
            )
            self._move_to_temp(work_path)
            return None

        if result is None or not result.recording_id:
            self._log.info("[DELETE] %s — Grund: kein AcoustID-Treffer", work_path.name)
            self._cleanup_file(work_path)
            return None

        if result.score < self._settings.acoustid_min_score:
            self._log.info(
                "[DELETE] %s — Grund: Score %.2f < Mindestwert %.2f",
                work_path.name,
                result.score,
                self._settings.acoustid_min_score,
            )
            self._cleanup_file(work_path)
            return None

        return result

    async def _enrich_parallel(
        self,
        result: FingerprintResult,
        track: TrackInfo,
        work_path: Path,
    ) -> tuple[EnrichedInfo | None, bytes | None, bytes | None, str | None]:
        """Führt iTunes, Lyrics und Artist-Image parallel aus."""
        enriched: EnrichedInfo | None = None
        cover_from_enrich: bytes | None = None
        artist_image: bytes | None = None
        lyrics: str | None = None

        async def _fetch_itunes() -> None:
            nonlocal enriched, cover_from_enrich
            try:
                enriched = await self._metadata.fetch(result.artist, result.title)
                if enriched and enriched.artwork_url:
                    cover_from_enrich = await self._metadata.download_image(enriched.artwork_url)
            except Exception:
                self._log.debug("[%s] iTunes enrichment failed for %s", self._name, work_path.name)

        async def _fetch_lyr() -> None:
            nonlocal lyrics
            if self._lyrics_provider is None:
                return
            lyrics = await _fetch_lyrics(
                self._lyrics_provider,
                track.artist,
                track.title,
                self._log,
                self._name,
            )

        async def _fetch_art_img() -> None:
            nonlocal artist_image
            if not result.artist or not self._popularity:
                return
            artist_image = await _fetch_artist_image(
                self._popularity,
                result.artist,
                self._name,
                self._log,
            )

        await asyncio.gather(_fetch_itunes(), _fetch_lyr(), _fetch_art_img())
        return enriched, cover_from_enrich, artist_image, lyrics

    async def _compute_destination_and_score(
        self,
        result: FingerprintResult,
        track: TrackInfo,
        enriched: EnrichedInfo | None,
        work_path: Path,
    ) -> tuple[str, Path | None, Path | None]:
        """Berechnet Zielpfad + Katalog-basierten Versionsvergleich.

        Returns (provenance, final_path, delete_old).
        delete_old ist None wenn keine alte Datei ersetzt wird.

        Der Zielpfad-Move + die Pfad-Duplikat-Erkennung (gleicher Zielpfad)
        übernimmt :meth:`_finalize_and_move` — hier wird nur die Katalog-Abfrage
        via ``recording_id`` geprüft (gleiche MBID → bessere Version gewinnt).
        """
        provenance = f"{self._name}/{self._name}"
        album = enriched.album if enriched and enriched.album else None
        final_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            track.stream_title,
            album=album,
        )

        delete_old: Path | None = None

        # ── Schritt 1: Katalog-basierter Versionsvergleich ──
        # Gleiche recording_id = definitiv derselbe Song (unabhängig von ISRC/Album).
        # Alle bekannten Kopien werden geprüft — ist auch nur eine besser, wird die neue verworfen.
        # Ist die neue besser, werden ALLE vorhandenen Kopien gelöscht (keine Duplikate).
        if self._catalog is not None and result.recording_id:
            existing_versions = await self._catalog.find_by_recording_id(result.recording_id)
            to_delete: list[Path] = []
            for ev in existing_versions:
                if ev.file_path == str(final_path):
                    continue  # wird über Pfad-Fallback in _finalize_and_move behandelt
                candidate = Path(ev.file_path)
                if not candidate.exists():
                    # Verwaister DB-Eintrag — sofort bereinigen
                    self._log.info(
                        "[%s] Verwaister Katalog-Eintrag (Datei fehlt) — bereinige: %s",
                        self._name,
                        ev.file_path,
                    )
                    await self._catalog.remove(ev.file_path)
                    continue
                if not is_better_version(
                    result.score,
                    None,
                    None,
                    ev.acoustid_score,
                    ev.bitrate,
                    ev.sample_rate,
                ):
                    # Mindestens eine vorhandene Kopie ist besser oder gleich → neue verwerfen
                    self._log.info(
                        "[DELETE] %s — Grund: Katalog-Treffer mit gleicher/besserer Version (score=%.4f >= %.4f)",
                        work_path.name,
                        ev.acoustid_score or 0.0,
                        result.score,
                    )
                    return "", None, None
                # Neue ist besser — diese Kopie für Löschung vormerken
                to_delete.append(candidate)
                self._log.info(
                    "[%s] Katalog-Treffer: ersetze %s (score=%.4f) durch bessere Version (%.4f)",
                    self._name,
                    candidate.name,
                    ev.acoustid_score or 0.0,
                    result.score,
                )
            # Erste zu löschende Datei als delete_old (wird nach Move gelöscht + DB bereinigt),
            # alle weiteren sofort löschen — verhindert mehrere Kopien desselben Songs.
            if to_delete:
                delete_old = to_delete[0]
                for extra in to_delete[1:]:
                    self._log.info(
                        "[DELETE] %s — Grund: weiteres Duplikat derselben recording_id",
                        extra.name,
                    )
                    safe_unlink(extra, parents_root=self._settings.destination)
                    await self._catalog.remove(str(extra))

        return provenance, final_path, delete_old

    async def _collect_metadata(
        self,
        result: FingerprintResult,
        track_pre: TrackInfo,
        work_path: Path,
    ) -> CollectedMetadata:
        """Phase 3+4: Parallele Anreicherung + MB-Korrektur (gemeinsam für neue & Bestandsdateien).

        CAA+MB || iTunes+Lyrics+ArtImg || Deezer laufen parallel, danach MB-Korrektur
        (MB-Daten kanonisch, iTunes als Fallback) inkl. Deezer-Re-Fetch.
        """
        # deezer_attempted unterscheidet "Deezer angerufen, kein Treffer" (→ löschen)
        # von "Deezer gar nicht versucht / Exception" (→ überspringen, API evtl. down)
        deezer_attempted = False

        async def _phase1_caa_mb() -> tuple[bytes | None, MusicBrainzData | None]:
            if not self._cover_provider or not result.recording_id:
                return None, None
            cover, mb, _ = await _fetch_cover_data(
                self._cover_provider,
                result.recording_id,
                None,
                "",
                self._name,
                self._log,
            )
            return cover, mb

        async def _phase_deezer() -> tuple[DeezerData | None, bytes | None]:
            nonlocal deezer_attempted
            if not self._deezer_provider or not (result.artist or result.title):
                return None, None
            try:
                data = await self._deezer_provider.fetch(result.artist, result.title)
                deezer_attempted = True
                cover = data.cover_bytes if data else None
                return data, cover
            except Exception:
                self._log.debug("[%s] Deezer fetch failed", self._name)
                return None, None

        phase1_task = asyncio.create_task(_phase1_caa_mb())
        phase3_task = asyncio.create_task(self._enrich_parallel(result, track_pre, work_path))
        deezer_task = asyncio.create_task(_phase_deezer())
        cover_from_caa, mb_data = await phase1_task
        enriched, cover_from_enrich, artist_image, lyrics = await phase3_task
        deezer_data, deezer_cover = await deezer_task

        # ── Phase 4: MB-Korrektur (MB-Daten sind kanonisch, iTunes als Fallback) ──
        corrected = correct_fingerprint_result(result, mb_data, enriched)
        if corrected is not result:
            self._log.info(
                "[%s] MB corrected artist/title: %s -> %s / %s -> %s",
                self._name,
                result.artist,
                corrected.artist,
                result.title,
                corrected.title,
            )
            # MB hat artist/title geändert → Deezer war für "falschen" Künstler gesucht → neu fetchen
            if (
                deezer_data is not None
                and (corrected.artist != result.artist or corrected.title != result.title)
                and self._deezer_provider is not None
            ):
                try:
                    deezer_data = await self._deezer_provider.fetch(corrected.artist, corrected.title)
                    deezer_attempted = True
                    deezer_cover = deezer_data.cover_bytes if deezer_data else None
                except Exception:
                    self._log.debug("[%s] Deezer re-fetch after MB correction failed", self._name)
                    deezer_data = None
                    deezer_cover = None
                    # Netzwerkfehler ≠ "nicht auf Deezer": Datei NICHT als unbekannt löschen.
                    deezer_attempted = False
            result = corrected

        # TrackInfo direkt aus den korrigierten Feldern bauen — kein Re-Split.
        track = TrackInfo(
            stream_title=f"{result.artist} - {result.title}",
            artist=result.artist,
            title=result.title,
        )
        return CollectedMetadata(
            result=result,
            track=track,
            enriched=enriched,
            mb_data=mb_data,
            deezer_data=deezer_data,
            deezer_cover=deezer_cover,
            cover_from_caa=cover_from_caa,
            cover_from_enrich=cover_from_enrich,
            deezer_attempted=deezer_attempted,
            artist_image=artist_image,
            lyrics=lyrics,
        )

    async def _process_file(self, proc_path: Path) -> None:
        work_path = await self._move_to_work_dir(proc_path)
        if work_path is None:
            return

        result = await self._fingerprint_and_validate(work_path)
        if result is None:
            return

        # ── Phase 3+4: Parallele Anreicherung + MB-Korrektur ──
        track_pre = TrackInfo(
            stream_title=f"{result.artist} - {result.title}",
            artist=result.artist,
            title=result.title,
        )
        meta = await self._collect_metadata(result, track_pre, work_path)

        # ── Phase 5-10: unter recording_id-Lock -- verhindert Race Condition ──
        # Zwei Worker die denselben Song gleichzeitig verarbeiten würden sonst beide
        # "kein Duplikat" lesen und beide schreiben. Der Lock serialisiert die
        # kritische Sequenz: Duplikat-Prüfung → Move → DB-Upsert.
        recording_lock = self._recording_locks.setdefault(meta.result.recording_id, asyncio.Lock())
        async with recording_lock:
            await self._process_critical_section(meta=meta, work_path=work_path)

    async def _process_critical_section(
        self,
        *,
        meta: CollectedMetadata,
        work_path: Path,
    ) -> None:
        """Kritischer Abschnitt unter recording_id-Lock: Duplikat-Prüfung, Move, Tagging, DB-Upsert."""
        result = meta.result
        track = meta.track
        enriched = meta.enriched
        deezer_data = meta.deezer_data
        deezer_attempted = meta.deezer_attempted

        provenance, final_path, _delete_old = await self._compute_destination_and_score(
            result,
            track,
            enriched,
            work_path,
        )
        if final_path is None:  # bestehende Datei hat besseren Score
            self._cleanup_file(work_path)
            return

        # ── Phase 6: .untested → .mp3 umbenennen ──
        stage_path = _strip_untested_suffix(work_path, self._log, self._name, on_fail="")
        if stage_path is None:
            self._cleanup_file(work_path)
            return

        # ── Phase 7: Popularitäts-Prüfung (Deezer-Rank) ──
        if self._settings.min_popularity_rank > 0 and (result.artist or result.title):
            if deezer_attempted and deezer_data is None:
                # Deezer angerufen, 0 Treffer → "nicht auf Deezer" → löschen
                safe_unlink(stage_path)
                self._log.warning(
                    "[%s] Deleted unknown track (not on Deezer): %s",
                    self._name,
                    stage_path.name,
                )
                return
            if not deezer_attempted:
                # Deezer call fehlgeschlagen (API down) oder Provider fehlt → nicht löschen
                self._log.debug("[%s] Skip popularity check (Deezer not attempted)", self._name)
            elif deezer_data is not None:
                rank = deezer_data.rank
                if rank is None:
                    # Sollte nicht passieren, aber defensiv
                    self._log.warning(
                        "[%s] Deezer treffer ohne rank? Behalte: %s",
                        self._name,
                        stage_path.name,
                    )
                elif rank < self._settings.min_popularity_rank:
                    safe_unlink(stage_path)
                    self._log.warning(
                        "[%s] Deleted unpopular track (rank=%d < min=%d): %s",
                        self._name,
                        rank,
                        self._settings.min_popularity_rank,
                        stage_path.name,
                    )
                    return
                else:
                    self._log.info(
                        "[%s] Popularity rank OK — %s / %s = %d",
                        self._name,
                        result.artist,
                        result.title,
                        rank,
                    )

        # ── Phase 8: Tag-Write (Cover: Deezer → CAA → iTunes) ──
        if not await self._write_tags(
            stage_path=stage_path,
            meta=meta,
            provenance=provenance,
            final_path=final_path,
        ):
            self._move_to_temp(stage_path)
            return

        # ── Phase 9: Atomarer Move zu destination + ggf. alte Datei löschen ──
        # _finalize_and_move behandelt: Ziel frei → verschieben; gleicher Song →
        # bessere Version gewinnt; Kollision (anderer Song) → verwerfen + Error-Log.
        await self._finalize_and_move(meta=meta, source_path=work_path, staged_path=stage_path)

        # Eviction nach erfolgreichem Abschluss
        if self._catalog is not None:
            await self._maybe_evict(
                meta.deezer_data.rank if meta.deezer_data else None,
                final_path=final_path,
            )

    async def _write_tags(
        self,
        *,
        stage_path: Path,
        meta: CollectedMetadata,
        provenance: str,
        final_path: Path,
    ) -> bool:
        """Phase 8: Ein-Pass-Tag-Write mit Retry. Gibt True bei Erfolg zurück."""
        result = meta.result
        # Cover-Priorität: CAA (verifiziert via recording_id) → Deezer → iTunes.
        final_cover = meta.cover_from_caa or meta.deezer_cover or meta.cover_from_enrich
        if final_cover is None:
            self._log.warning(
                "[%s] Kein Cover für %s gefunden (CAA/Deezer/iTunes lieferten nichts). "
                "Gesucht mit: artist=%r title=%r recording_id=%r",
                self._name,
                stage_path.name,
                result.artist,
                result.title,
                result.recording_id,
            )

        last_exc: Exception | None = None
        for attempt in range(1, _TAG_WRITE_MAX_ATTEMPTS + 1):
            try:
                self._tagger.write_all(
                    stage_path,
                    meta.track,
                    provenance,
                    enriched=meta.enriched,
                    cover_bytes=final_cover,
                    recording_id=result.recording_id,
                    score=result.score,
                    mb_data=meta.mb_data,
                    artist_image=meta.artist_image,
                    lyrics=meta.lyrics,
                    deezer=meta.deezer_data,
                    artist_mbid=result.artist_mbid,
                )
                last_exc = None
                break
            except Exception as exc:
                last_exc = exc
                if attempt < _TAG_WRITE_MAX_ATTEMPTS:
                    self._log.warning(
                        "[%s] Tag write fehlgeschlagen (Versuch %d/%d), retry: %s",
                        self._name,
                        attempt,
                        _TAG_WRITE_MAX_ATTEMPTS,
                        exc,
                    )
                    await asyncio.sleep(_TAG_WRITE_BASE_DELAY * attempt)

        if last_exc is not None:
            self._log.warning(
                "[%s] Tag write failed — Datei wird NICHT nach %s verschoben (ungetaggt): %s",
                self._name,
                final_path.parent,
                last_exc,
            )
            return False

        if final_cover is not None:
            self._log.info("[%s] Cover embedded: %s", self._name, stage_path.name)
        if meta.lyrics:
            self._log.info(
                "[%s] Lyrics found for %s (%d chars)",
                self._name,
                stage_path.name,
                len(meta.lyrics),
            )
        return True

    async def _catalog_upsert(
        self,
        final_path: Path,
        result: FingerprintResult,
        mb_data: MusicBrainzData | None,
        enriched: EnrichedInfo | None,
        deezer_data: DeezerData | None,
        *,
        has_cover: bool,
        has_performer: bool = False,
    ) -> None:
        """Liest Audio-Eigenschaften nach dem Move und trägt das Lied im Katalog ein."""
        assert self._catalog is not None
        try:
            audio = await asyncio.to_thread(read_audio_from_file, final_path)
        except Exception:
            self._log.debug("[%s] Audio-Lesen für Katalog fehlgeschlagen: %s", self._name, final_path)
            audio = {"bitrate": None, "sample_rate": None, "duration_ms": None}
        isrc = None
        if deezer_data and deezer_data.isrc:
            isrc = deezer_data.isrc
        elif mb_data and mb_data.isrcs:
            isrc = mb_data.isrcs[0]
        rec = SongRecord(
            file_path=str(final_path),
            recording_id=result.recording_id,
            isrc=isrc,
            artist=result.artist,
            title=result.title,
            album=enriched.album if enriched else None,
            genre=enriched.genre if enriched else None,
            release_group_type=mb_data.release_group_type if mb_data else None,
            station_name=self._name,
            file_size=final_path.stat().st_size if final_path.exists() else None,
            bitrate=audio["bitrate"],
            sample_rate=audio["sample_rate"],
            duration_ms=audio["duration_ms"],
            acoustid_score=result.score,
            popularity_rank=deezer_data.rank if deezer_data else None,
            has_cover=has_cover,
            has_performer=has_performer,
        )
        try:
            await self._catalog.upsert(rec)
        except Exception:
            self._log.warning("[%s] Katalog-Upsert fehlgeschlagen: %s", self._name, final_path)

    async def _maybe_evict(self, new_rank: int | None, *, final_path: Path) -> None:
        """Verdrängt das unpopulärste Lied, wenn das Sammlungslimit erreicht ist."""
        if not self._catalog or not self._settings.enable_eviction:
            return
        if self._settings.max_collection_size <= 0:
            return
        try:
            current = await self._catalog.count()
        except Exception:
            self._log.debug("[%s] Katalog-Count fehlgeschlagen", self._name)
            return
        if current < self._settings.max_collection_size:
            return
        try:
            candidates = await self._catalog.find_least_popular(limit=20)
        except Exception:
            self._log.debug("[%s] find_least_popular fehlgeschlagen", self._name)
            return

        # Primär: Song verdrängen, der weniger populär ist als der neue
        victim = pick_eviction_candidate(candidates, new_rank) if new_rank is not None else None
        # Fallback: absolut unpopulärsten Song verdrängen (neuen ausschließen)
        if victim is None:
            candidates.sort(key=lambda r: r.popularity_rank if r.popularity_rank is not None else 2**31 - 1)
            for c in candidates:
                if c.file_path != str(final_path):
                    victim = c
                    break
        if victim is None:
            return
        victim_path = Path(victim.file_path)
        self._log.info(
            "[EVICT] %s — Grund: Rank %s < neuer Rank %s (Sammlung bei Limit %d)",
            victim.file_path,
            victim.popularity_rank,
            new_rank,
            self._settings.max_collection_size,
        )
        safe_unlink(victim_path, parents_root=self._settings.destination)
        try:
            await self._catalog.remove(victim.file_path)
        except Exception:
            self._log.debug("[%s] Katalog-Remove nach Eviction fehlgeschlagen", self._name)


__all__ = ["FileProcessor"]
