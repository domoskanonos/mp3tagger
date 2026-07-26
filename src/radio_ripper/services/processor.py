"""File processor — single-worker inbox processing for recorded MP3s.

Scans an ``inbox`` (mp3_inbox) for ``.mp3`` files and processes them one by
one: fingerprint → enrich → CAA → MB → popularity → lyrics → ONE tag write,
all in a ``work_dir`` staging area, then atomically moved to ``destination/``.
No database involved — files are either perfect (in destination/) or deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from radio_ripper.domain.models import EnrichedInfo, FingerprintResult, MusicBrainzData, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.lyrics import LyricsProvider
from radio_ripper.services.metadata import CoverArtProvider, MetadataProvider
from radio_ripper.services.popularity import PopularityProvider, maybe_delete_obscure
from radio_ripper.services.storage import (
    compute_file_path,
    read_acoustid_score,
    safe_unlink,
)
from radio_ripper.services.tagging import TrackTagger


# ── helpers ──


def _untested_rename(
    file_path: Path,
    logger: logging.Logger,
    station_name: str,
    *,
    on_fail: str | None = None,
) -> Path | None:
    new_path = file_path.with_name(file_path.stem.replace(".untested", "") + ".mp3")
    if file_path == new_path:
        return file_path
    if new_path.exists():
        logger.warning(
            "[%s] Refuse to rename %s -> %s (target exists).%s",
            station_name, file_path.name, new_path.name,
            on_fail or " Keeping .untested.mp3 for manual review.",
        )
        return None
    try:
        file_path.rename(new_path)
        return new_path
    except OSError as exc:
        logger.warning(
            "[%s] rename %s -> %s failed: %s",
            station_name, file_path.name, new_path.name, exc,
        )
        return None


def correct_fingerprint_result(
    result: FingerprintResult,
    mb_data: MusicBrainzData | None,
) -> FingerprintResult:
    if mb_data and mb_data.recording_artist and mb_data.recording_title:
        if (
            result.artist.lower() != mb_data.recording_artist.lower()
            or result.title.lower() != mb_data.recording_title.lower()
        ):
            return FingerprintResult(
                artist=mb_data.recording_artist,
                title=mb_data.recording_title,
                score=result.score,
                recording_id=result.recording_id,
            )
    return result


async def _fetch_artist_image(
    popularity_provider: PopularityProvider,
    artist: str,
    station_name: str,
    logger: logging.Logger,
) -> bytes | None:
    try:
        img = await popularity_provider.fetch_artist_image(artist)
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
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return (
        results[0] if not isinstance(results[0], BaseException) else None,
        results[1] if not isinstance(results[1], BaseException) else None,
        results[2] if len(results) > 2 and not isinstance(results[2], BaseException) else None,
    )


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


# ── processor ──


class FileProcessor:
    """Single-worker inbox processor.

    Polls *inbox* for ``.mp3`` files, processes each sequentially:
      1. Move ``.processing`` to ``work_dir/``.
      2. Fingerprint (AcoustID) — score < min → delete.
      3. Remux + enrich (iTunes) + basic tags.
      4. Score-compare with existing file at destination.
      5. CAA cover, MusicBrainz metadata, artist image, popularity.
      6. Lyrics.
      7. Atomic rename to ``destination/``.
    If ANY step fails the staging file is deleted; ``destination/`` is never
    touched until the file is fully processed.
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
        lyrics_provider: LyricsProvider | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._inbox = inbox
        self._temp_dir = temp_dir
        self._name = name
        self._poll_interval = poll_interval
        self._log = logger or logging.getLogger(f"radio_ripper.{name}")
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._settings = settings
        self._fingerprint = fingerprint_provider
        self._metadata = metadata_provider
        self._tagger = tagger
        self._cover_provider = cover_provider
        self._popularity = popularity_provider
        self._lyrics_provider = lyrics_provider

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

    # ── polling loop ──

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

    # ── main file processing ──

    async def _process_file(self, proc_path: Path) -> None:
        # ── Move .processing from inbox to work_dir for safe staging ──
        work_path = self._settings.work_dir / proc_path.name
        try:
            work_path.parent.mkdir(parents=True, exist_ok=True)
            proc_path.rename(work_path)
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben ins work_dir fehlgeschlagen",
                proc_path.name,
            )
            self._cleanup_file(proc_path)
            return

        # ── Fingerprint ──
        try:
            result = await self._fingerprint.fingerprint(work_path)
        except NonRetriableFingerprintError:
            self._log.warning("[DELETE] %s — Grund: Datei korrupt/nicht lesbar", work_path.name)
            self._cleanup_file(work_path)
            return
        except FingerprintError:
            self._log.warning(
                "[TEMP] %s — Grund: Fingerprint-Infrastrukturfehler, verschoben zur Prüfung",
                work_path.name,
            )
            self._move_to_temp(work_path)
            return

        if result is None or not result.recording_id:
            self._log.info("[DELETE] %s — Grund: kein AcoustID-Treffer", work_path.name)
            self._cleanup_file(work_path)
            return

        if result.score < self._settings.acoustid_min_score:
            self._log.info(
                "[DELETE] %s — Grund: Score %.2f < Mindestwert %.2f",
                work_path.name,
                result.score,
                self._settings.acoustid_min_score,
            )
            self._cleanup_file(work_path)
            return

        # ── Phase 1: CAA + MB + Deezer Artist Image parallel ──
        mb_data: MusicBrainzData | None = None
        cover_from_caa: bytes | None = None
        _artist_img_for_caa: bytes | None = None
        if self._cover_provider and result.recording_id:
            cover_from_caa, mb_data, _artist_img_for_caa = await _fetch_cover_data(
                self._cover_provider,
                result.recording_id,
                self._popularity,
                result.artist or "",
                self._name,
                self._log,
            )

        # ── Phase 2: MB-Korrektur (Artist/Title Swap-Fix, MB ist kanonisch) ──
        corrected = correct_fingerprint_result(result, mb_data)
        if corrected is not result:
            self._log.info(
                "[%s] MB corrected artist/title: %s -> %s / %s -> %s",
                self._name,
                result.artist, corrected.artist,
                result.title, corrected.title,
            )
            result = corrected
        stream_title = f"{result.artist} - {result.title}"
        track = TrackInfo.from_stream_title(stream_title)

        # ── Phase 3: iTunes + Lyrics + Artist-Image parallel (mit korrigierten Werten) ──
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

        async def _fetch_lyrics_wrapper() -> None:
            nonlocal lyrics
            if self._lyrics_provider is None:
                return
            lyrics = await _fetch_lyrics(
                self._lyrics_provider, track.artist, track.title, self._log, self._name,
            )

        async def _fetch_artist_image() -> None:
            nonlocal artist_image
            if not self._popularity or not result.artist:
                return
            try:
                artist_image = await self._popularity.fetch_artist_image(result.artist)
            except Exception:
                self._log.debug("[%s] Artist image failed for %s", self._name, work_path.name)

        await asyncio.gather(_fetch_itunes(), _fetch_lyrics_wrapper(), _fetch_artist_image())

        # ── Phase 4: Ziel-Pfad berechnen + Score-basierte Entscheidung ──
        provenance = f"{self._name}/{self._name}"
        album = enriched.album if enriched and enriched.album else None
        final_path = compute_file_path(
            self._settings.destination,
            result.artist,
            result.title,
            stream_title,
            album=album,
        )

        delete_old: Path | None = None
        if final_path.exists():
            existing_score = read_acoustid_score(final_path)
            if existing_score is not None and existing_score >= result.score:
                self._log.info(
                    "[DELETE] %s — Grund: existierende Datei hat besseren/gleichen "
                    "Score (%.4f >= %.4f)",
                    work_path.name,
                    existing_score,
                    result.score,
                )
                self._cleanup_file(work_path)
                return
            self._log.info(
                "Neue Datei hat besseren Score (%.4f > %.4f) — "
                "alte wird nach erfolgreichem Move gelöscht",
                result.score,
                existing_score or 0.0,
            )
            delete_old = final_path

        # ── Rename .untested.mp3 → .mp3 ──
        stage_path = _untested_rename(work_path, self._log, self._name, on_fail="")
        if stage_path is None:
            self._cleanup_file(work_path)
            return

        # ── Popularität (Deezer) – löscht Datei bei zu unbekannt ──
        if self._settings.min_popularity_rank > 0 and self._popularity and (result.artist or result.title):
            deleted = await maybe_delete_obscure(
                file_path=stage_path,
                station_name=self._name,
                artist=result.artist,
                title=result.title,
                min_rank=self._settings.min_popularity_rank,
                popularity_provider=self._popularity,
                logger=self._log,
            )
            if deleted:
                return

        # ── EIN Tag-Schreib-Durchgang ──
        final_cover = cover_from_caa or cover_from_enrich
        try:
            self._tagger.write_all(
                stage_path,
                track,
                provenance,
                enriched=enriched,
                cover_bytes=final_cover,
                recording_id=result.recording_id,
                score=result.score,
                mb_data=mb_data,
                artist_image=artist_image,
                lyrics=lyrics,
            )
            if final_cover is not None:
                self._log.info("[%s] Cover embedded: %s", self._name, stage_path.name)
            if lyrics:
                self._log.info(
                    "[%s] Lyrics found for %s (%d chars)",
                    self._name, stage_path.name, len(lyrics),
                )
        except Exception as exc:
            self._log.warning("[%s] Tag write failed: %s", self._name, exc)

        # ── Atomarer Move zu destination ──
        final_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            stage_path.rename(final_path)
        except OSError:
            self._log.error(
                "[DELETE] %s — Grund: Verschieben nach %s fehlgeschlagen",
                stage_path.name, final_path,
            )
            self._cleanup_file(stage_path)
            return

        self._log.info("[%s] Fertig: %s", self._name, final_path)

        # ── Alte Datei erst jetzt löschen (neue ist sicher am Ziel) ──
        if delete_old is not None:
            self._log.info(
                "[DELETE] %s — Grund: durch bessere Version ersetzt",
                delete_old.name,
            )
            safe_unlink(delete_old, parents_root=self._settings.destination)


__all__ = ["FileProcessor"]
