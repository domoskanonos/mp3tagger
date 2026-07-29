"""Pydantic settings models for radio_ripper (tag)."""

from __future__ import annotations

import json
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
    enable_coverartarchive: bool = True
    max_concurrent: int = Field(default=3, ge=1, le=20)

    # ── Collection-Management ───────────────────────────────────────────────
    catalog_db: Path = Field(default=Path("./work/catalog.db"))
    reconcile_on_startup: bool = True
    max_collection_size: int = Field(
        default=0, ge=0, description="0=disabled. >0 aktiviert Größenlimit + Eviction-Logik"
    )
    enable_eviction: bool = Field(default=False, description="Verdränge unpopulärste Songs bei vollem Sammlungslimit")
    exclude_release_group_types: list[str] = Field(
        default_factory=lambda: ["Live", "Bootleg"],
        description="MusicBrainz Release Group Types die sofort aussortiert werden",
    )
    exclude_title_patterns: list[str] = Field(
        default_factory=list, description="Case-insensitive Substrings, z.B. ['(live', 'live at', 'concert']"
    )

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("work_dir", "destination", "log_file", "source", "catalog_db")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


def load_settings(path: str | Path) -> Settings:
    cfg_path = Path(path).expanduser()
    if not cfg_path.is_file():
        raise ConfigurationError(f"config file not found: {cfg_path}")
    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read config {cfg_path}: {exc}") from exc
    try:
        return Settings.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"invalid config: {exc}") from exc


__all__ = ["Settings", "load_settings"]
