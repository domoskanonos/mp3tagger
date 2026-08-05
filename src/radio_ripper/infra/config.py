"""Pydantic settings models for radio_ripper (tag)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from radio_ripper.infra.errors import ConfigurationError


class Settings(BaseModel):
    model_config = {"populate_by_name": True}

    destination: Path = Field(default=Path("./destination"))
    work_dir: Path = Field(default=Path("./work"))

    log_level: str = "INFO"
    log_file: Path = Field(default=Path("./work/radio_ripper.log"))

    metadata_timeout: float = Field(default=8.0, ge=0.5)
    cover_timeout: float = Field(default=15.0, ge=0.5)

    source: Path = Field(default=Path("./source"))
    acoustid_min_score: float = Field(default=0.85, ge=0.0, le=1.0)
    min_popularity_rank: int = Field(default=100000, ge=0)
    max_concurrent: int = Field(default=3, ge=1)
    # Parallelität für das Durchsuchen der Bestandsdateien beim Enrich
    # (Kandidaten-Scan). Unabhängig von max_concurrent, da es nur Tags liest
    # und keine API-Calls macht — ein stabiler, schneller Wert ist hier sicher.
    scan_concurrency: int = Field(default=20, ge=1)

    # ── Collection-Management ───────────────────────────────────────────────
    reconcile_on_startup: bool = True
    # Vervollständigt nach dem Reconcile fehlende Tags/Cover in Bestandsdateien
    # (gleicher Anreicherungs-Flow wie bei neuen MP3s). Nur Dateien mit fehlenden
    # Feldern werden verarbeitet.
    enrich_missing_tags_on_startup: bool = False
    max_collection_size: int = Field(
        default=0, ge=0, description="0=disabled. >0 aktiviert Größenlimit + Eviction-Logik"
    )
    enable_eviction: bool = Field(default=False, description="Verdränge unpopulärste Songs bei vollem Sammlungslimit")

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("work_dir", "destination", "log_file", "source")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


def _strip_jsonc_comments(text: str) -> str:
    """Entfernt // Zeilenkommentare aus JSONC-Text."""
    return re.sub(r"//[^\n]*", "", text)


def load_settings(path: str | Path) -> Settings:
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigurationError(f"config file not found: {cfg_path}")
    try:
        raw_text = cfg_path.read_text(encoding="utf-8")
        raw = json.loads(_strip_jsonc_comments(raw_text))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read config {cfg_path}: {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid config: {exc}") from exc


__all__ = ["Settings", "load_settings"]
