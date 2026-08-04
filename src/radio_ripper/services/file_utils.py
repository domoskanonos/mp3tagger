"""Datei- und Pfad-Utilities für die Tagging-Pipeline."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")

# Linux-Limit pro Pfad-Segment: 255 Bytes (UTF-8).
# Wir reservieren 4 Bytes für ".mp3" → max 251 Bytes für den Basis-Dateinamen.
_MAX_FILENAME_BYTES = 251
# Einzelne Komponenten (Artist, Title) auf 120 Zeichen begrenzen.
_MAX_COMPONENT_CHARS = 120


def sanitize_filename(name: str | None) -> str:
    if name is None:
        return "unknown"
    name = name.strip()
    if not name:
        return "unknown"
    name = name.replace("\r", " ").replace("\n", " ")
    name = _ILLEGAL_FILENAME_CHARS.sub("", name)
    name = _WHITESPACE_RE.sub(" ", name).strip()
    if not name:
        return "unknown"
    if len(name) > _MAX_COMPONENT_CHARS:
        name = name[:_MAX_COMPONENT_CHARS].strip()
    return name or "unknown"


def _fit_to_byte_limit(stem: str, limit: int = _MAX_FILENAME_BYTES) -> str:
    """Kürzt *stem* so, dass ``stem.encode('utf-8')`` ≤ *limit* Bytes lang ist."""
    encoded = stem.encode("utf-8")
    if len(encoded) <= limit:
        return stem
    # Byteweise kürzen und dann sauber als UTF-8 dekodieren
    truncated = encoded[:limit]
    return truncated.decode("utf-8", errors="ignore").rstrip()


def compute_file_path(
    destination: Path,
    artist: str,
    title: str,
    stream_title_clean: str,
    *,
    album: str | None = None,
) -> Path:
    """Berechnet den Zielpfad: Künstler[/Album]/Künstler - Titel.mp3"""
    if artist and title:
        artist_san = sanitize_filename(artist)
        title_san = sanitize_filename(title)
        base = _fit_to_byte_limit(f"{artist_san} - {title_san}")
    else:
        artist_san = "Unknown"
        base = _fit_to_byte_limit(sanitize_filename(stream_title_clean))
    parent = destination / artist_san / sanitize_filename(album) if album else destination / artist_san
    return parent / f"{base}.mp3"


def safe_unlink(path: Path, *, parents_root: Path | None = None) -> None:
    """Löscht *path* und optional leere Elternverzeichnisse bis *parents_root*.
    Fehler werden still ignoriert."""
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)
        if parents_root is not None:
            remove_empty_parents(path, parents_root)


def remove_empty_parents(file_path: Path, root: Path) -> None:
    child = file_path.parent
    while child != root:
        try:
            child.rmdir()
        except OSError:
            break
        child = child.parent


__all__ = [
    "compute_file_path",
    "remove_empty_parents",
    "safe_unlink",
    "sanitize_filename",
]
