"""Datei- und Pfad-Utilities für die Tagging-Pipeline."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


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
    if len(name) > 200:
        name = name[:200].strip()
    return name or "unknown"


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
        artist_dir = sanitize_filename(artist)
        base = f"{sanitize_filename(artist)} - {sanitize_filename(title)}"
    else:
        artist_dir = "Unknown"
        base = sanitize_filename(stream_title_clean)
    parent = destination / artist_dir / sanitize_filename(album) if album else destination / artist_dir
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
