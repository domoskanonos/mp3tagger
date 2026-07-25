"""File IO + path utilities for the tagging pipeline."""

from __future__ import annotations

import contextlib
import re
from datetime import UTC, datetime
from pathlib import Path

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE_RE = re.compile(r"\s+")


def sanitize_filename(name: str) -> str:
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
    overwrite: bool = False,
) -> Path:
    if artist and title:
        artist_dir = sanitize_filename(artist)
        base = f"{sanitize_filename(artist)} - {sanitize_filename(title)}"
    else:
        artist_dir = "Unknown"
        base = sanitize_filename(stream_title_clean)
    if album:
        parent = destination / artist_dir / sanitize_filename(album)
    else:
        parent = destination / artist_dir
    candidate = parent / f"{base}.mp3"
    if not overwrite and candidate.exists():
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S_%f")
        candidate = parent / f"{base}_{ts}.mp3"
    return candidate


def remux_mp3(path: Path) -> None:
    tmp = path.with_suffix(".remux.tmp")
    try:
        from pydub import AudioSegment

        audio = AudioSegment.from_file(str(path), format="mp3")
        audio.export(str(tmp), format="mp3", tags={})
        tmp.replace(path)
    except ImportError:
        pass
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)


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
    "remux_mp3",
    "sanitize_filename",
]
