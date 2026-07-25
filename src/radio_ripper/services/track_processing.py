"""Shared post-recording track processing pipeline.

Extracted from :class:`~radio_ripper.services.stream.StreamRecorder` so that
both live-stream recordings and inbox-uploaded files run through the exact
same registration, enrichment, tagging, filing, and fingerprinting logic.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path

from radio_ripper.domain.models import EnrichedInfo, FingerprintResult, SavedTrack, TrackInfo
from radio_ripper.infra.config import Settings
from radio_ripper.services.fingerprint import (
    FingerprintError,
    FingerprintProvider,
    NonRetriableFingerprintError,
)
from radio_ripper.services.metadata import CoverArtProvider, MetadataProvider
from radio_ripper.services.popularity import PopularityProvider, maybe_delete_obscure
from radio_ripper.services.repository import TrackRepository
from radio_ripper.services.storage import remove_empty_parents, sanitize_filename
from radio_ripper.services.tagging import TrackTagger, enrich_and_tag

_LOGGER = logging.getLogger("radio_ripper.track_processing")


def _lock_for(path: Path, locks: dict[Path, asyncio.Lock]) -> asyncio.Lock:
    lock = locks.get(path)
    if lock is None:
        lock = asyncio.Lock()
        locks[path] = lock
    return lock


def _release_lock(path: Path, locks: dict[Path, asyncio.Lock]) -> None:
    locks.pop(path, None)


def _safe_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Enrichment
# ---------------------------------------------------------------------------


async def enrich_song(
    file_path: Path,
    track: TrackInfo,
    provenance: str,
    settings: Settings,
    metadata_provider: MetadataProvider | None,
    tagger: TrackTagger,
    enrich_semaphore: asyncio.Semaphore | None = None,
    file_locks: dict[Path, asyncio.Lock] | None = None,
    logger: logging.Logger = _LOGGER,
) -> EnrichedInfo | None:
    """Fetch metadata enrichment and write ID3 tags.

    Acquires the optional semaphore and per-file lock to serialise enrichment
    vs fingerprint writes on the same file.
    """
    if metadata_provider is None:
        return None
    sem = enrich_semaphore
    locks = file_locks or {}
    try:
        if sem is not None:
            await sem.acquire()
        lock = _lock_for(file_path, locks)
        async with lock:
            info = await enrich_and_tag(
                metadata_provider,
                tagger,
                file_path,
                track,
                provenance,
                fallback_cover_path=settings.fallback_cover_path,
                embed_cover_art=True,
                logger=logger,
            )
            if info is not None:
                logger.info(
                    "[%s] Enriched: %s | album=%s year=%s cover=%s",
                    provenance,
                    file_path.name,
                    info.album or "-",
                    info.year or "-",
                    "yes" if info.artwork_url else "no",
                )
            else:
                logger.info(
                    "[%s] no enrichment hit for: %s - %s",
                    provenance,
                    track.artist,
                    track.title,
                )
            return info
    except Exception:
        logger.exception("[%s] enrichment failed for %s", provenance, file_path.name)
        return None
    finally:
        if sem is not None:
            sem.release()


# ---------------------------------------------------------------------------
# Enrich + tag + file to album subfolder (no DB)
# ---------------------------------------------------------------------------


async def enrich_and_file(
    file_path: Path,
    track: TrackInfo,
    station_name: str,
    provenance: str,
    settings: Settings,
    tagger: TrackTagger,
    metadata_provider: MetadataProvider | None = None,
    enrich_semaphore: asyncio.Semaphore | None = None,
    file_locks: dict[Path, asyncio.Lock] | None = None,
    logger: logging.Logger = _LOGGER,
) -> Path | None:
    """Write basic ID3 tags, enrich via iTunes, move to album subfolder.

    Like :func:`register_and_enrich` but without any database operations.
    Returns the final (potentially album-moved) path, or ``None`` on error.
    """
    try:
        tagger.write_basic(file_path, track, provenance)
    except Exception as exc:
        logger.warning("[%s] basic tag failed: %s", station_name, exc)

    info: EnrichedInfo | None = None
    if metadata_provider:
        info = await enrich_song(
            file_path,
            track,
            provenance,
            settings,
            metadata_provider,
            tagger,
            enrich_semaphore=enrich_semaphore,
            file_locks=file_locks,
            logger=logger,
        )

    if info and info.album:
        artist_dir = sanitize_filename(info.artist or track.artist)
        album_dir = sanitize_filename(info.album)
        new_dir = settings.destination / artist_dir / album_dir
        new_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_dir / file_path.name
        try:
            shutil.move(str(file_path), str(new_path))
            remove_empty_parents(file_path, settings.destination)
            file_path = new_path
        except OSError as exc:
            logger.warning("[%s] album dir move failed: %s", station_name, exc)

    logger.info(
        "[%s] Completed: %s (%d bytes)",
        station_name,
        file_path.name,
        _safe_size(file_path),
    )
    return file_path


# ---------------------------------------------------------------------------
# Register + enrich + tag + file to album subfolder (with DB)
# ---------------------------------------------------------------------------


async def register_and_enrich(
    file_path: Path,
    track: TrackInfo,
    station_name: str,
    provenance: str,
    settings: Settings,
    repo: TrackRepository,
    tagger: TrackTagger,
    metadata_provider: MetadataProvider | None = None,
    enrich_semaphore: asyncio.Semaphore | None = None,
    file_locks: dict[Path, asyncio.Lock] | None = None,
    logger: logging.Logger = _LOGGER,
) -> Path | None:
    """Register in DB, write basic ID3 tags, enrich, move to album subdir.

    Returns *final_path* (potentially moved into an album subfolder after
    enrichment), or ``None`` if the file was discarded.
    """
    early_path = file_path
    fsize = _safe_size(early_path)
    try:
        await repo.register(
            SavedTrack(
                stream_title=track.stream_title,
                artist=track.artist,
                title=track.title,
                file_path=str(early_path),
                file_size=fsize,
            ),
            station_name,
        )
    except Exception as exc:
        logger.warning("[%s] early db-register: %s", station_name, exc)

    try:
        tagger.write_basic(file_path, track, provenance)
    except Exception as exc:
        logger.warning("[%s] tag failed: %s", station_name, exc)

    info: EnrichedInfo | None = None
    if metadata_provider:
        info = await enrich_song(
            file_path,
            track,
            provenance,
            settings,
            metadata_provider,
            tagger,
            enrich_semaphore=enrich_semaphore,
            file_locks=file_locks,
            logger=logger,
        )

    if info and info.album:
        artist_dir = sanitize_filename(info.artist or track.artist)
        album_dir = sanitize_filename(info.album)
        new_dir = settings.destination / artist_dir / album_dir
        new_dir.mkdir(parents=True, exist_ok=True)
        new_path = new_dir / file_path.name
        try:
            shutil.move(str(file_path), str(new_path))
            remove_empty_parents(file_path, settings.destination)
            file_path = new_path
        except OSError as exc:
            logger.warning("[%s] album dir move failed (race?): %s", station_name, exc)

    fsize2 = _safe_size(file_path)
    try:
        await repo.update_enrichment(
            station_name,
            track.stream_title,
            artist=info.artist if info and info.artist else None,
            title=info.title if info and info.title else None,
            album=info.album if info else None,
            year=info.year if info else None,
            file_size=fsize2,
            has_cover=(info is not None),
            enrichment="itunes" if info else "",
            label=info.label if info else None,
            track_number=info.track_number if info else None,
            disc_number=info.disc_number if info else None,
        )
    except Exception as exc:
        logger.warning("[%s] db update enrichment: %s", station_name, exc)

    if file_path != early_path:
        try:
            await repo.update_file_path(station_name, track.stream_title, str(file_path))
        except Exception as exc:
            logger.warning("[%s] db update_file_path: %s", station_name, exc)

    logger.info(
        "[%s] Completed: %s (%d bytes)",
        station_name,
        file_path.name,
        fsize2,
    )
    return file_path


# ---------------------------------------------------------------------------
# Fingerprint + apply match
# ---------------------------------------------------------------------------


async def fingerprint_song(
    file_path: Path,
    track: TrackInfo,
    station_name: str,
    provenance: str,
    settings: Settings,
    fingerprint_provider: FingerprintProvider | None,
    repo: TrackRepository,
    tagger: TrackTagger,
    cover_provider: CoverArtProvider | None = None,
    popularity_provider: PopularityProvider | None = None,
    file_locks: dict[Path, asyncio.Lock] | None = None,
    logger: logging.Logger = _LOGGER,
    precomputed_result: FingerprintResult | None = None,
) -> None:
    """Fingerprint a recorded file and apply the match result.

    When *precomputed_result* is provided (e.g. from a prior AcoustID lookup
    in the uploader), the fingerprint call is skipped and the existing result
    is used directly.  All other steps (rename, CAA, popularity, dedup) run
    the same as for a fresh match.

    Handles:
      - AcoustID lookup (skipped when *precomputed_result* is set)
      - Rename ``.untested.mp3`` → ``.mp3`` on match
      - Cover Art Archive fetch and embed
      - Popularity check / deletion of obscure tracks (Deezer)
      - Cross-station AcoustID dedup (keep highest-scoring copy)
      - Cleanup of unmatched duplicates when a matched version appears
    """
    if fingerprint_provider is None and precomputed_result is None:
        return

    locks = file_locks or {}
    lock = _lock_for(file_path, locks)
    try:
        async with lock:
            result: FingerprintResult | None
            if precomputed_result is not None:
                result = precomputed_result
            else:
                try:
                    result = await fingerprint_provider.fingerprint(file_path)  # type: ignore[union-attr]
                except NonRetriableFingerprintError as exc:
                    logger.warning(
                        "[%s] deleting broken file %s: %s",
                        station_name,
                        file_path.name,
                        exc,
                    )
                    with contextlib.suppress(OSError):
                        file_path.unlink(missing_ok=True)
                    with contextlib.suppress(Exception):
                        await repo.remove(station_name, track.stream_title)
                    return
                except FingerprintError as exc:
                    logger.warning(
                        "[%s] fingerprint infrastructure error for %s: %s (file kept as .untested.mp3 for retry)",
                        station_name,
                        file_path.name,
                        exc,
                        exc_info=True,
                    )
                    return
                except Exception:
                    logger.debug(
                        "[%s] unexpected fingerprint error for %s",
                        station_name,
                        file_path.name,
                    )
                    return

            if result is None:
                logger.info("[%s] No AcoustID match: %s", station_name, file_path.name)
                if track.artist and track.title:
                    try:
                        all_artist_title = await repo.find_all_by_artist_title(
                            track.artist,
                            track.title,
                        )
                    except Exception:
                        all_artist_title = []
                    has_matched = any(
                        e.track.acoustid_recording_id
                        for e in all_artist_title
                        if not (
                            e.station_name == station_name
                            and e.track.stream_title.lower() == track.stream_title.lower()
                        )
                    )
                    if has_matched:
                        logger.info(
                            "[%s] AcoustID unmatched, but a matched version already exists — discarding new: %s",
                            station_name,
                            file_path.name,
                        )
                        with contextlib.suppress(OSError):
                            file_path.unlink(missing_ok=True)
                            remove_empty_parents(file_path, settings.destination)
                        try:
                            await repo.remove(station_name, track.stream_title)
                        except Exception as exc:
                            logger.debug(
                                "[%s] db remove after fallback-dup: %s",
                                station_name,
                                exc,
                            )
                        return

                with contextlib.suppress(OSError):
                    file_path.unlink(missing_ok=True)
                    remove_empty_parents(file_path, settings.destination)
                with contextlib.suppress(Exception):
                    await repo.remove(station_name, track.stream_title)
                logger.info(
                    "[%s] Discarded (no AcoustID match): %s",
                    station_name,
                    file_path.name,
                )
                return

            logger.info(
                "[%s] AcoustID match (score=%.2f): %s - %s (rec=%s)",
                station_name,
                result.score,
                result.artist,
                result.title,
                result.recording_id,
            )
            new_path = file_path.with_name(file_path.stem.replace(".untested", "") + ".mp3")
            applied = await apply_fingerprint_match(
                recording_id=result.recording_id,
                score=result.score,
                file_path=file_path,
                new_path=new_path,
                tagger=tagger,
                cover_provider=cover_provider,
                repository=repo,
                station_name=station_name,
                stream_title=track.stream_title,
                logger=logger,
                artist=result.artist,
                title=result.title,
                popularity_provider=popularity_provider,
                min_popularity_rank=settings.min_popularity_rank,
            )
            if applied is None:
                return
            new_path = applied

            if not result.recording_id:
                return

            try:
                all_existing = await repo.find_all_by_recording_id(result.recording_id)
            except Exception as exc:
                logger.debug("[%s] find_all_by_recording_id: %s", station_name, exc)
                return

            if all_existing:
                candidates: list[tuple[float, str, str, Path]] = [
                    (
                        e.track.acoustid_score or 0.0,
                        e.station_name,
                        e.track.stream_title,
                        Path(e.track.file_path),
                    )
                    for e in all_existing
                ]
                candidates.append((result.score, station_name, track.stream_title, new_path))
                candidates.sort(key=lambda c: c[0], reverse=True)
                (best_score, best_station, best_stream, best_path) = candidates[0]
                for score, station, stream_title, p in candidates:
                    if (score, station, stream_title, p) == (
                        best_score,
                        best_station,
                        best_stream,
                        best_path,
                    ):
                        continue
                    logger.info(
                        "[%s] AcoustID dedup: discarding inferior (score %.2f < best %.2f): %s",
                        station_name,
                        score,
                        best_score,
                        p.name,
                    )
                    with contextlib.suppress(OSError):
                        p.unlink(missing_ok=True)
                        remove_empty_parents(p, settings.destination)
                    try:
                        await repo.remove(station, stream_title)
                    except Exception as exc:
                        logger.debug("[%s] db remove dedup: %s", station_name, exc)

            if track.artist and track.title:
                try:
                    unmatched = await repo.find_all_by_artist_title(
                        track.artist,
                        track.title,
                    )
                except Exception:
                    unmatched = []
                for rec in unmatched:
                    if (
                        rec.station_name == station_name
                        and rec.track.stream_title.lower() == track.stream_title.lower()
                    ):
                        continue
                    if rec.track.acoustid_recording_id:
                        continue
                    logger.info(
                        "[%s] Replacing unmatched recording with matched version: %s",
                        station_name,
                        rec.track.file_path,
                    )
                    old_path = Path(rec.track.file_path)
                    with contextlib.suppress(OSError):
                        old_path.unlink(missing_ok=True)
                        remove_empty_parents(old_path, settings.destination)
                    try:
                        await repo.remove(rec.station_name, rec.track.stream_title)
                    except Exception as exc:
                        logger.debug(
                            "[%s] db remove unmatched for replacement: %s",
                            station_name,
                            exc,
                        )
    finally:
        _release_lock(file_path, locks)


async def apply_fingerprint_match(
    *,
    recording_id: str,
    score: float,
    file_path: Path,
    new_path: Path,
    tagger: TrackTagger,
    cover_provider: CoverArtProvider | None,
    repository: TrackRepository,
    station_name: str,
    stream_title: str,
    logger: logging.Logger,
    artist: str = "",
    title: str = "",
    popularity_provider: PopularityProvider | None = None,
    min_popularity_rank: int = 0,
) -> Path | None:
    """Rename after AcoustID match, update ID3 tags + DB, fetch CAA cover.

    When *min_popularity_rank* > 0 and *popularity_provider* is set, also
    checks track popularity via Deezer and deletes the file + DB record if
    the rank is below the threshold.

    Returns *new_path* on success or ``None`` when rename was refused
    (target exists) or the file was deleted (too obscure).
    All steps are best-effort — failures are logged and never reraised.
    """
    if file_path != new_path:
        if new_path.exists():
            logger.warning(
                "[%s] Refuse to rename %s -> %s (target exists). Keeping .untested.mp3 for manual review.",
                station_name,
                file_path.name,
                new_path.name,
            )
            return None
        try:
            file_path.rename(new_path)
        except OSError as exc:
            logger.warning(
                "[%s] rename %s -> %s failed: %s",
                station_name,
                file_path.name,
                new_path.name,
                exc,
            )
            return None

    logger.info("[%s] AcoustID match applied: %s", station_name, new_path.name)

    try:
        tagger.update_acoustid(new_path, recording_id, score)
    except Exception as exc:
        logger.debug("[%s] acoustid tag update: %s", station_name, exc)
    try:
        await repository.update_file_path(station_name, stream_title, str(new_path))
    except Exception as exc:
        logger.debug("[%s] db update_file_path: %s", station_name, exc)
    try:
        await repository.update_fingerprint(
            station_name,
            stream_title,
            recording_id=recording_id,
            score=score,
        )
    except Exception as exc:
        logger.debug("[%s] db update_fingerprint: %s", station_name, exc)

    if recording_id and cover_provider is not None:
        try:
            cover_bytes = await cover_provider.fetch_cover_by_recording_id(recording_id)
        except Exception as exc:
            logger.debug(
                "[%s] Cover Art Archive lookup failed: %s",
                station_name,
                exc,
            )
            cover_bytes = None
        if cover_bytes is not None:
            try:
                tagger.embed_cover(new_path, cover_bytes)
                logger.info(
                    "[%s] Embedded CAA cover: %s",
                    station_name,
                    new_path.name,
                )
            except Exception as exc:
                logger.debug(
                    "[%s] embed CAA cover failed: %s",
                    station_name,
                    exc,
                )
        else:
            logger.info(
                "[%s] No CAA cover available for recording %s",
                station_name,
                recording_id,
            )

        # Fetch MusicBrainz metadata (label, ISRC, length, release info)
        try:
            mb_data = await cover_provider.fetch_recording_data(recording_id)
        except Exception as exc:
            logger.debug(
                "[%s] MusicBrainz recording data lookup failed: %s",
                station_name,
                exc,
            )
            mb_data = None
        if mb_data is not None:
            try:
                tagger.update_musicbrainz_metadata(new_path, mb_data)
                if mb_data.release_label:
                    logger.info(
                        "[%s] MB label: %s",
                        station_name,
                        mb_data.release_label,
                    )
            except Exception as exc:
                logger.debug(
                    "[%s] update_musicbrainz_metadata failed: %s",
                    station_name,
                    exc,
                )

        # Fetch & embed artist portrait from Deezer
        if popularity_provider is not None and artist:
            try:
                img = await popularity_provider.fetch_artist_image(artist)
                if img is not None:
                    tagger.write_artist_image(new_path, img)
                    logger.info(
                        "[%s] Artist image embedded: %s",
                        station_name,
                        artist,
                    )
            except Exception as exc:
                logger.debug(
                    "[%s] artist image fetch failed: %s",
                    station_name,
                    exc,
                )

    if min_popularity_rank > 0 and popularity_provider is not None and (artist or title):
        deleted = await maybe_delete_obscure(
            file_path=new_path,
            station_name=station_name,
            stream_title=stream_title,
            artist=artist,
            title=title,
            min_rank=min_popularity_rank,
            popularity_provider=popularity_provider,
            repository=repository,
            logger=logger,
        )
        if deleted:
            return None

    return new_path


__all__ = [
    "apply_fingerprint_match",
    "enrich_and_file",
    "enrich_song",
    "fingerprint_song",
    "register_and_enrich",
]
