#!/usr/bin/env python3
"""Standalone script: tag one or more MP3 files in-place.

Fingerprints via AcoustID, enriches via iTunes + CAA, embeds cover art,
writes lyrics, and tags with a single mutagen save.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_proj_root = Path(__file__).resolve().parent
sys.path.insert(0, str(_proj_root))

from radio_ripper.infra.config import Settings
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.infra.logging import configure_logging
from radio_ripper.services.fingerprint import AcoustidFingerprintProvider, FingerprintResult
from radio_ripper.services.lyrics import LRCLibProvider
from radio_ripper.services.metadata import CoverArtArchiveProvider, ITunesMetadataProvider
from radio_ripper.services.popularity import DeezerPopularityChecker
from radio_ripper.services.tagging import ID3Tagger
from radio_ripper.services.track_processing import correct_fingerprint_result
from radio_ripper.domain.models import EnrichedInfo, MusicBrainzData, TrackInfo


def load_env(env_path: Path) -> None:
    if not env_path.is_file():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def extract_artist_title_from_path(path: Path) -> tuple[str, str]:
    """Guess artist/title from a path like ``Artist/Album/Artist - Title.mp3``."""
    stem = path.stem
    for sep in (" - ", " — "):
        if sep in stem:
            artist, _, title = stem.partition(sep)
            return artist.strip(), title.strip()
    return "", ""


async def tag_single_file(
    file_path: Path,
    settings: Settings,
    fingerprint: AcoustidFingerprintProvider,
    metadata: ITunesMetadataProvider,
    tagger: ID3Tagger,
    cover_archive: CoverArtArchiveProvider | None,
    popularity: DeezerPopularityChecker | None,
    logger: logging.Logger,
) -> bool:
    """Tag a single MP3 file in-place. Returns True on success."""
    # ── Fingerprint ──
    result: FingerprintResult | None = None
    try:
        result = await fingerprint.fingerprint(file_path)
    except Exception as exc:
        logger.warning("Fingerprint failed for %s: %s", file_path.name, exc)
        return False

    if result is None:
        logger.warning("No AcoustID match for %s", file_path.name)
        return False
    if not result.recording_id:
        logger.warning("No recording ID for %s", file_path.name)
        return False

    artist = result.artist
    title = result.title
    logger.info(
        "AcoustID match (score=%.2f): %s - %s (rec=%s)",
        result.score, artist, title, result.recording_id,
    )

    # ── Phase 1: CAA + MB parallel (nur recording_id-abhängig) ──
    mb_data: MusicBrainzData | None = None
    cover_from_caa: bytes | None = None
    if cover_archive and result.recording_id:
        import asyncio as _a

        caa_task = cover_archive.fetch_cover_by_recording_id(result.recording_id)
        mb_task = cover_archive.fetch_recording_data(result.recording_id)
        cov_results = await _a.gather(caa_task, mb_task, return_exceptions=True)
        cover_from_caa = cov_results[0] if not isinstance(cov_results[0], BaseException) else None
        mb_data = cov_results[1] if not isinstance(cov_results[1], BaseException) else None

    # ── Phase 2: MB-Korrektur (Artist/Title Swap-Fix, MB ist kanonisch) ──
    corrected = correct_fingerprint_result(result, mb_data)
    if corrected is not result:
        logger.info(
            "MB corrected artist/title: %s -> %s / %s -> %s",
            result.artist, corrected.artist,
            result.title, corrected.title,
        )
        result = corrected
        artist = result.artist
        title = result.title
    track = TrackInfo.from_stream_title(f"{artist} - {title}")

    # ── Phase 3: iTunes + Lyrics + Artist-Image parallel (mit korrigierten Werten) ──
    enriched: EnrichedInfo | None = None
    cover_from_enrich: bytes | None = None
    artist_image: bytes | None = None
    lyrics: str | None = None
    import asyncio as _a

    async def _fetch_itunes() -> None:
        nonlocal enriched, cover_from_enrich
        if not metadata:
            return
        try:
            enriched = await metadata.fetch(artist, title)
            if enriched and enriched.artwork_url:
                cover_from_enrich = await metadata.download_image(enriched.artwork_url)
        except Exception as exc:
            logger.debug("iTunes enrichment failed: %s", exc)

    async def _fetch_lyrics() -> None:
        nonlocal lyrics
        try:
            lyrics_provider = LRCLibProvider(HttpxAsyncClient(), timeout=5.0)
            lyrics = await lyrics_provider.fetch(artist, title)
        except Exception as exc:
            logger.debug("Lyrics fetch failed: %s", exc)

    async def _fetch_artist_image() -> None:
        nonlocal artist_image
        if not popularity or not artist:
            return
        try:
            artist_image = await popularity.fetch_artist_image(artist)
        except Exception as exc:
            logger.debug("Artist image fetch failed: %s", exc)

    await _a.gather(_fetch_itunes(), _fetch_lyrics(), _fetch_artist_image())

    # ── Phase 4: Merge + EIN Tag-Schreib-Durchgang ──
    provenance = "tag-file/standalone"
    final_cover = cover_from_caa or cover_from_enrich
    try:
        tagger.write_all(
            file_path,
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
            logger.info("Cover embedded: %s", file_path.name)
        if lyrics:
            logger.info("Lyrics found (%d chars)", len(lyrics))
        logger.info("Tagged: %s", file_path.name)
        return True
    except Exception as exc:
        logger.warning("Tag write failed for %s: %s", file_path.name, exc)
        return False


async def _main(files: list[Path], settings: Settings, logger: logging.Logger) -> int:
    api_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACCOUST_ID", "")
    if not api_key:
        logger.critical("ACOUSTID_API_KEY not set")
        return 1

    client = HttpxAsyncClient()

    fingerprint = AcoustidFingerprintProvider(api_key, min_score=settings.acoustid_min_score)
    metadata = ITunesMetadataProvider(client, metadata_timeout=settings.metadata_timeout)
    tagger = ID3Tagger()

    cover_archive: CoverArtArchiveProvider | None = None
    if settings.enable_coverartarchive:
        cover_archive = CoverArtArchiveProvider(client, timeout=settings.cover_timeout)

    popularity: DeezerPopularityChecker | None = None
    if settings.min_popularity_rank > 0:
        popularity = DeezerPopularityChecker(client)

    success = 0
    for fp in files:
        if not fp.is_file():
            logger.warning("Skipping (not a file): %s", fp)
            continue
        if fp.suffix.lower() not in (".mp3",):
            logger.debug("Skipping (not mp3): %s", fp)
            continue
        ok = await tag_single_file(
            fp, settings, fingerprint, metadata, tagger,
            cover_archive, popularity, logger,
        )
        if ok:
            success += 1

    await client.aclose()
    logger.info("Done: %d/%d files tagged", success, len(files))
    return 0 if success == len(files) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Tag MP3 files in-place using AcoustID + iTunes + CAA.",
    )
    parser.add_argument("paths", nargs="+", help="MP3 files or directories to scan")
    parser.add_argument("--config", default=None, help="Path to config.json")
    parser.add_argument("--log-level", default=None, help="Log level")
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    # Load .env
    load_env(_proj_root / ".env")

    # Config
    cfg_path: str | None = args.config
    if cfg_path is None:
        cfg_path = str(_proj_root / "config.json")
    settings = Settings.model_validate_json(Path(cfg_path).read_bytes())

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})

    logger = configure_logging(settings.log_level, settings.log_file)

    # Collect files
    files: list[Path] = []
    for p in args.paths:
        pp = Path(p).expanduser().resolve()
        if pp.is_dir():
            files.extend(sorted(pp.rglob("*.mp3")))
        elif pp.is_file():
            files.append(pp)
    if not files:
        logger.warning("No MP3 files found.")
        return 0

    logger.info("Found %d MP3 file(s)", len(files))
    try:
        return asyncio.run(_main(files, settings, logger))
    except KeyboardInterrupt:
        logger.info("Interrupted.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
