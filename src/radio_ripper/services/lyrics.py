"""Lyrics providers — fetch song lyrics from public APIs.

The :class:`LyricsProvider` ABC lets the ripper swap providers.
:class:`LyricsOvhProvider` uses the free `lyrics.ovh <https://lyrics.ovh>`_ API
which requires no API key.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from radio_ripper.infra.http import AsyncHttpClient

_log = logging.getLogger(__name__)

_LYRICS_OVH_URL = "https://api.lyrics.ovh/v1/{artist}/{title}"
# Pattern: strip feat./ft./and etc. from song titles for lyrics lookup
_FEAT_RE = re.compile(
    r"\s*[(\[]?(?:feat\.|ft\.?|featuring|vs\.?)\s+\S.*?[)\]]?\s*$",
    re.IGNORECASE,
)
_PAREN_RE = re.compile(r"\s*\([^)]*\)\s*$")


def _clean_title(title: str) -> str:
    """Remove feat./ft./parenthetical from *title* for lyrics lookup."""
    title = _FEAT_RE.sub("", title)
    title = _PAREN_RE.sub("", title)
    return title.strip()


class LyricsProvider(ABC):
    """Fetch lyrics text for a given artist + title."""

    @abstractmethod
    async def fetch(self, artist: str, title: str) -> str | None:
        """Return lyrics text or ``None`` when not found."""


class LyricsOvhProvider(LyricsProvider):
    """lyrics.ovh API — free, no API key required."""

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, artist: str, title: str) -> str | None:
        if not artist or not title:
            return None
        clean_artist = artist.strip()
        clean_title = _clean_title(title)
        url = _LYRICS_OVH_URL.format(artist=clean_artist, title=clean_title)
        _log.info("Fetching lyrics from %s", url)
        try:
            payload = await self._client.get_json(url, timeout=self._timeout)
        except Exception:
            _log.debug("lyrics.ovh fetch failed for %s - %s", clean_artist, clean_title)
            return None
        text: str | None = (payload or {}).get("lyrics")
        if text is not None:
            text = text.strip()
        if text:
            _log.info("Lyrics found: %d chars for %s - %s", len(text), clean_artist, clean_title)
        return text or None


__all__ = ["LyricsOvhProvider", "LyricsProvider", "_clean_title"]
