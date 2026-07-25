"""Tests for radio_ripper.infra.logging."""

from __future__ import annotations

import logging
from pathlib import Path

from radio_ripper.infra.logging import configure_logging


def test_configure_returns_logger():
    lg = configure_logging(level="DEBUG")
    assert lg.name == "radio_ripper"
    assert lg.level == logging.DEBUG


def test_handlers_added():
    lg = configure_logging(level="INFO")
    assert any(isinstance(h, logging.StreamHandler) for h in lg.handlers)


def test_file_handler_created(tmp_path: Path):
    log_file = tmp_path / "subdir" / "ripper.log"
    lg = configure_logging(level="INFO", log_file=log_file)
    lg.info("hello")
    for h in lg.handlers:
        h.flush()
    assert log_file.is_file()
    assert "hello" in log_file.read_text(encoding="utf-8")


def test_idempotent_no_duplicate_handlers():
    lg = configure_logging(level="INFO")
    n = len(lg.handlers)
    lg2 = configure_logging(level="INFO")
    assert lg is lg2
    assert len(lg2.handlers) == n
