"""Künstler-Bilder über die öffentliche Deezer-API.

Kein API-Key nötig — Deezer's Such-Endpunkt ist öffentlich.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from radio_ripper.infra.http import AsyncHttpClient

_LOGGER = logging.getLogger(__name__)


class PopularityProvider(ABC):
    """Liefert Künstler-Bilder."""

    @abstractmethod
    async def fetch_artist_image(self, artist: str) -> bytes | None:
        """Künstler-Porträt als Bytes oder ``None``."""


class DeezerPopularityChecker(PopularityProvider):
    """Holt Künstler-Bilder über die öffentliche Deezer-Suche."""

    _ARTIST_SEARCH_URL = "https://api.deezer.com/search/artist"

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch_artist_image(self, artist: str) -> bytes | None:
        """Deezer-Künstlerbild abrufen."""
        if not artist:
            return None
        try:
            payload = await self._client.get_json(
                self._ARTIST_SEARCH_URL,
                params={"q": artist, "limit": 1},
                timeout=self._timeout,
            )
        except Exception:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            return None
        picture_url = data[0].get("picture_medium") or data[0].get("picture")
        if not picture_url:
            return None
        try:
            return await self._client.get_bytes(picture_url, timeout=self._timeout)
        except Exception:
            return None


__all__ = [
    "DeezerPopularityChecker",
    "PopularityProvider",
]
