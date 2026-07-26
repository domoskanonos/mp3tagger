"""CLI entry point for radio-ripper tag."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from radio_ripper import __version__
from radio_ripper.infra.config import Settings, load_settings
from radio_ripper.infra.errors import ConfigurationError
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.infra.logging import configure_logging
from radio_ripper.services.fingerprint import AcoustidFingerprintProvider
from radio_ripper.services.lyrics import LRCLibProvider
from radio_ripper.services.metadata import CoverArtArchiveProvider, ITunesMetadataProvider
from radio_ripper.services.popularity import DeezerPopularityChecker, PopularityProvider
from radio_ripper.services.processor import FileProcessor
from radio_ripper.services.tagging import ID3Tagger

_LOGGER = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radio-ripper-tag")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=None, help="Config file path")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser


async def _run(settings: Settings, logger: logging.Logger) -> int:
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    client = HttpxAsyncClient()

    api_key = os.environ.get("ACOUSTID_API_KEY") or os.environ.get("ACCOUST_ID", "")
    if not api_key:
        logger.critical("ACOUSTID_API_KEY not set — cannot run without fingerprinting.")
        logger.critical("Set ACOUSTID_API_KEY (or ACCOUST_ID) in .env or environment.")
        return 1
    fp = AcoustidFingerprintProvider(api_key, min_score=settings.acoustid_min_score)

    metadata = ITunesMetadataProvider(client, metadata_timeout=settings.metadata_timeout)
    tagger = ID3Tagger()
    inbox = settings.mp3_inbox or settings.work_dir / "mp3_inbox"

    popularity: PopularityProvider | None = None
    if settings.min_popularity_rank and settings.min_popularity_rank > 0:
        popularity = DeezerPopularityChecker(client)

    cover_archive: CoverArtArchiveProvider | None = None
    if settings.enable_coverartarchive:
        cover_archive = CoverArtArchiveProvider(client, timeout=settings.cover_timeout)

    lyrics_provider = LRCLibProvider(client, timeout=5.0)

    proc = FileProcessor(
        inbox=inbox,
        temp_dir=settings.work_dir / "failed",
        settings=settings,
        fingerprint_provider=fp,
        metadata_provider=metadata,
        tagger=tagger,
        name="tag",
        poll_interval=2.0,
        cover_provider=cover_archive,
        popularity_provider=popularity,
        lyrics_provider=lyrics_provider,
        logger=logger,
    )

    def _signal_handler(signum: int, _frame: object | None) -> None:
        logger.info("Signal %s received - shutting down...", signum)
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _signal_handler, signal.SIGINT, None)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler, signal.SIGTERM, None)

    await proc.start()
    try:
        await stop_event.wait()
    finally:
        await proc.stop()
        await client.aclose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cfg_path: str | None = args.config
    if cfg_path is None or not Path(cfg_path).expanduser().is_file():
        print("No config found. Use --config PATH.", file=sys.stderr)
        return 2

    try:
        settings = load_settings(cfg_path)
    except ConfigurationError as exc:
        print(f"Failed to load config: {exc}", file=sys.stderr)
        return 2

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})

    logger = configure_logging(settings.log_level, settings.log_file)
    logger.info("=== Radio-Ripper %s (tag mode) ===", __version__)

    try:
        return asyncio.run(_run(settings, logger))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shut down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
