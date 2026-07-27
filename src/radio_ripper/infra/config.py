"""Pydantic settings models for radio_ripper (tag)."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from radio_ripper.infra.errors import ConfigurationError


class Settings(BaseModel):
    model_config = {"populate_by_name": True}

    destination: Path = Field(default=Path("./recordings"))
    work_dir: Path = Field(default=Path("./work"))

    log_level: str = "INFO"
    log_file: Path | None = None

    metadata_timeout: float = Field(default=8.0, ge=0.5)
    cover_timeout: float = Field(default=15.0, ge=0.5)

    mp3_inbox: Path | None = Field(default=None, alias="mp3_inbox")
    acoustid_min_score: float = Field(default=0.85, ge=0.0, le=1.0)
    min_popularity_rank: int = Field(default=100000, ge=0)
    enable_coverartarchive: bool = True
    max_concurrent: int = Field(default=3, ge=1, le=20)

    @field_validator("log_level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"invalid log_level: {v}")
        return v

    @field_validator("work_dir", "destination", "log_file", "mp3_inbox")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None

    @model_validator(mode="after")
    def _resolve_work_paths(self) -> Settings:
        if self.log_file is None:
            self.log_file = self.work_dir / "radio_ripper.log"
        if self.mp3_inbox is None:
            self.mp3_inbox = Path("./mp3_inbox")
        return self


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
