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
    # Beste zuerst — größtes verfügbares Bild bevorzugen.
    _PICTURE_KEYS = ("picture_xl", "picture_big", "picture_medium", "picture")

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
                params={"q": artist, "limit": 5},
                timeout=self._timeout,
            )
        except Exception:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            return None
        # Mehrere Kandidaten durchgehen, bis ein Bild heruntergeladen werden kann.
        for artist_data in data:
            if not isinstance(artist_data, dict):
                continue
            picture_url = next(
                (artist_data[key] for key in self._PICTURE_KEYS if artist_data.get(key)),
                None,
            )
            if not picture_url:
                continue
            try:
                return await self._client.get_bytes(picture_url, timeout=self._timeout)
            except Exception:
                continue
        return None


__all__ = [
    "DeezerPopularityChecker",
    "PopularityProvider",
]
