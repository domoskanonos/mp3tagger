"""Centralised logging configuration."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def configure_logging(
    level: str = "INFO",
    log_file: Path | None = None,
) -> logging.Logger:
    """Configure the ``radio_ripper`` logger with console + optional rotating file handler.

    Idempotent: calling it repeatedly clears previous handlers (avoids duplicate
    log lines after a reconfigure or test).

    Args:
        level: Log level name (``DEBUG`` / ``INFO`` / ``WARNING`` / …).
        log_file: Optional path for a 5 MB rotating file handler
            (5 backups). Parent directories are created if needed.

    Returns:
        The configured ``radio_ripper`` logger instance.
    """
    logger = logging.getLogger("radio_ripper")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    logger.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


__all__ = ["configure_logging"]
