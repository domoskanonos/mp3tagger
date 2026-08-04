"""CLI entry point for radio-ripper tag."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from radio_ripper import __version__
from radio_ripper.infra.catalog import SqliteCatalog
from radio_ripper.infra.config import Settings, load_settings
from radio_ripper.infra.errors import ConfigurationError
from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.infra.logging import configure_logging
from radio_ripper.services.fingerprint import AcoustidFingerprintProvider
from radio_ripper.services.lyrics import LRCLibProvider
from radio_ripper.services.metadata_deezer import DeezerMetadataProvider
from radio_ripper.services.metadata_itunes import ITunesMetadataProvider
from radio_ripper.services.metadata_musicbrainz import CoverArtArchiveProvider
from radio_ripper.services.popularity import DeezerPopularityChecker, PopularityProvider
from radio_ripper.services.processor import FileProcessor
from radio_ripper.services.tagging import ID3Tagger

_LOGGER = logging.getLogger(__name__)


def _find_config(cfg_arg: str | None) -> Path | None:
    if cfg_arg:
        p = Path(cfg_arg).expanduser()
        if p.is_file():
            return p

    candidates = [
        Path("config/config.jsonc"),
        Path("config.jsonc"),
        Path.home() / ".config" / "radio-ripper" / "config.jsonc",
        Path("/app/config/config.jsonc"),
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="radio-ripper-tag")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("-c", "--config", default=None, help="Config file path")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"])
    return parser


async def _watch_config(
    cfg_path: Path,
    proc: FileProcessor,
    logger: logging.Logger,
    log_level: str,
) -> None:
    last_mtime = cfg_path.stat().st_mtime
    while True:
        await asyncio.sleep(10)
        try:
            mtime = cfg_path.stat().st_mtime
            if mtime > last_mtime:
                new_settings = load_settings(cfg_path)
                if new_settings.log_level != log_level:
                    logging.getLogger().setLevel(new_settings.log_level)
                    logger.setLevel(new_settings.log_level)
                    log_level = new_settings.log_level
                proc.reload_settings(new_settings)
                last_mtime = mtime
                logger.info("Config reloaded from %s (mtime changed)", cfg_path)
        except Exception:
            logger.warning("Config reload failed", exc_info=True)


async def _run_pipeline(settings: Settings, logger: logging.Logger, cfg_path: Path | None = None) -> int:
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
    inbox = settings.source if settings.source is not None else settings.work_dir / "source"

    popularity: PopularityProvider | None = None
    if settings.min_popularity_rank and settings.min_popularity_rank > 0:
        popularity = DeezerPopularityChecker(client)

    cover_archive: CoverArtArchiveProvider | None = None
    if settings.enable_coverartarchive:
        cover_archive = CoverArtArchiveProvider(client, timeout=settings.cover_timeout)

    lyrics_provider = LRCLibProvider(client, timeout=5.0)

    deezer_provider = DeezerMetadataProvider(client, timeout=settings.cover_timeout)

    catalog = SqliteCatalog(settings.work_dir / "catalog.db")
    if settings.reconcile_on_startup:
        logger.info("[Startup] Reconcile Katalog ⇄ Dateisystem ...")
        report = await catalog.reconcile_with_filesystem(settings.destination)
        logger.info(
            "[Startup] Reconcile fertig: %d added, %d removed, %d kept (gesamt: %d, dauer: %.1fs)",
            report.added,
            report.removed,
            report.kept,
            report.added + report.kept,
            report.duration_s,
        )

    proc = FileProcessor(
        inbox=inbox,
        temp_dir=settings.work_dir / "failed",
        settings=settings,
        fingerprint_provider=fp,
        metadata_provider=metadata,
        tagger=tagger,
        name="tag",
        poll_interval=300.0,
        cover_provider=cover_archive,
        deezer_provider=deezer_provider,
        popularity_provider=popularity,
        lyrics_provider=lyrics_provider,
        catalog=catalog,
        logger=logger,
    )

    def _signal_handler(signum: int, _frame: object | None) -> None:
        logger.info("Signal %s received - shutting down...", signum)
        stop_event.set()

    loop.add_signal_handler(signal.SIGINT, _signal_handler, signal.SIGINT, None)
    loop.add_signal_handler(signal.SIGTERM, _signal_handler, signal.SIGTERM, None)

    watch_task: asyncio.Task[None] | None = None
    if cfg_path:
        watch_task = asyncio.create_task(_watch_config(cfg_path, proc, logger, settings.log_level))

    await proc.start()
    try:
        await stop_event.wait()
    finally:
        await proc.stop()
        if watch_task:
            watch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watch_task
        await catalog.aclose()
        await client.aclose()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])

    cfg_path = _find_config(args.config)

    if cfg_path:
        try:
            settings = load_settings(cfg_path)
        except ConfigurationError as exc:
            print(f"Failed to load config: {exc}", file=sys.stderr)
            return 2
    else:
        settings = Settings()

    if args.log_level:
        settings = settings.model_copy(update={"log_level": args.log_level})

    logger = configure_logging(settings.log_level, settings.log_file)
    logger.info("=== Radio-Ripper %s (tag mode) ===", __version__)
    if cfg_path:
        logger.info("Config loaded from %s", cfg_path)
    else:
        logger.info("No config file found — using defaults")

    try:
        return asyncio.run(_run_pipeline(settings, logger, cfg_path))
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt — shut down.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
