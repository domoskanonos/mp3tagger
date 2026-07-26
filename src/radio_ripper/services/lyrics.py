"""Lyrics providers — fetch song lyrics from public APIs.

The :class:`LyricsProvider` ABC lets the ripper swap providers.
:class:`LRCLibProvider` uses the free `LRCLIB <https://lrclib.net>`_ API
which requires no API key.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod

from radio_ripper.infra.http import AsyncHttpClient

_log = logging.getLogger(__name__)

_LRCLIB_GET_URL = "https://lrclib.net/api/get"
_LRCLIB_SEARCH_URL = "https://lrclib.net/api/search"
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


class LRCLibProvider(LyricsProvider):
    """LRCLIB API — free, no API key required.

    Uses the ``/api/get`` exact-match endpoint and falls back to
    ``/api/search`` when no exact match is found.
    """

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(self, artist: str, title: str) -> str | None:
        if not artist or not title:
            return None
        clean_artist = artist.strip()
        clean_title = _clean_title(title)

        lyrics = await self._fetch_exact(clean_artist, clean_title)
        if lyrics is not None:
            return lyrics

        lyrics = await self._fetch_search(clean_artist, clean_title)
        if lyrics is not None:
            return lyrics

        _log.debug("LRCLIB: no lyrics for %s - %s", clean_artist, clean_title)
        return None

    async def _fetch_exact(self, artist: str, title: str) -> str | None:
        url = _LRCLIB_GET_URL
        params = {"artist_name": artist, "track_name": title}
        _log.debug("LRCLIB exact: %s %s", artist, title)
        try:
            payload = await self._client.get_json(url, params=params, timeout=self._timeout)
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("instrumental"):
            _log.debug("LRCLIB: %s - %s is instrumental, skipping", artist, title)
            return None
        text: str | None = payload.get("plainLyrics")
        if text is not None:
            text = text.strip()
        if text:
            _log.info("LRCLIB: lyrics found (%d chars) for %s - %s", len(text), artist, title)
        return text or None

    async def _fetch_search(self, artist: str, title: str) -> str | None:
        url = _LRCLIB_SEARCH_URL
        params = {"artist_name": artist, "track_name": title}
        _log.debug("LRCLIB search fallback: %s %s", artist, title)
        try:
            payload = await self._client.get_json(url, params=params, timeout=self._timeout)
        except Exception:
            return None
        if not isinstance(payload, list) or not payload:
            return None
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            if entry.get("instrumental"):
                continue
            text: str | None = entry.get("plainLyrics")
            if text is not None:
                text = text.strip()
            if text:
                _log.info(
                    "LRCLIB: lyrics found via search (%d chars) for %s - %s",
                    len(text),
                    artist,
                    title,
                )
                return text
        return None


__all__ = ["LRCLibProvider", "LyricsProvider", "_clean_title"]
