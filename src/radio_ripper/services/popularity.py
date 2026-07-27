"""Popularitäts-Prüfung über die öffentliche Deezer-API.

Liefert Track-Rang (für Popularitäts-Filter im Processor) und Künstler-Bilder.
Kein API-Key nötig — Deezer's Such-Endpunkt ist öffentlich.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod

import httpx

from radio_ripper.infra.http import AsyncHttpClient
from radio_ripper.infra.resilience import retry_async

_LOGGER = logging.getLogger(__name__)


class PopularityProvider(ABC):
    """Prüft Track-Popularität und liefert Künstler-Bilder."""

    @abstractmethod
    async def get_rank(self, artist: str, title: str) -> int | None:
        """Deezer-Popularitätsrang (je höher desto bekannter) oder ``None``."""

    @abstractmethod
    async def fetch_artist_image(self, artist: str) -> bytes | None:
        """Künstler-Porträt als Bytes oder ``None``."""


class DeezerPopularityChecker(PopularityProvider):
    """Ermittelt Popularität über die öffentliche Deezer-Suche.

    Sucht nach ``Künstler "Titel"`` und extrahiert den ``rank``-Wert
    aus dem ersten Treffer. Kein API-Key erforderlich.
    """

    _SEARCH_URL = "https://api.deezer.com/search"
    _ARTIST_SEARCH_URL = "https://api.deezer.com/search/artist"

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 5.0) -> None:
        self._client = client
        self._timeout = timeout

    @retry_async(max_attempts=2, base_delay=0.5, exceptions=(httpx.HTTPError,))
    async def get_rank(self, artist: str, title: str) -> int | None:  # type: ignore[override]
        q = f'{artist} "{title}"'
        try:
            payload = await self._client.get_json(self._SEARCH_URL, params={"q": q, "limit": 1}, timeout=self._timeout)
        except Exception:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not data:
            return None
        rank = data[0].get("rank")
        return int(rank) if rank is not None else None

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
