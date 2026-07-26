"""File IO + path utilities for the tagging pipeline."""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError

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
    return parent / f"{base}.mp3"


def read_acoustid_score(path: Path) -> float | None:
    try:
        audio = ID3(path)
    except Exception:
        return None
    for frame in audio.getall("TXXX"):
        if frame.desc == "AcoustID Score":
            try:
                return float(frame.text[0])
            except (ValueError, IndexError):
                return None
    return None


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
        safe_unlink(tmp)


def safe_unlink(path: Path, *, parents_root: Path | None = None) -> None:
    """Remove *path* and optionally its empty ancestor directories up to *parents_root*."""
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
    "read_acoustid_score",
    "remove_empty_parents",
    "remux_mp3",
    "safe_unlink",
    "sanitize_filename",
]
