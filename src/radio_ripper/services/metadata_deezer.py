"""Deezer-Album-Cover als Fallback-Quelle.

:class:`DeezerCoverProvider` sucht über die öffentliche Deezer Search API
nach Künstler/Titel und extrahiert das Album-Cover (250x250 px).

Kein API-Key erforderlich.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from radio_ripper.infra.http import AsyncHttpClient, download_image_or_none

DEEZER_SEARCH_URL = "https://api.deezer.com/search"


class CoverProvider(ABC):
    @abstractmethod
    async def fetch_cover(self, artist: str, title: str) -> bytes | None:
        """Album-Cover-Bytes für Künstler/Titel oder ``None``."""


class DeezerCoverProvider(CoverProvider):
    """Deezer Search API — Album-Cover als Fallback."""

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 8.0) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch_cover(self, artist: str, title: str) -> bytes | None:
        query = f"{artist} {title}".strip()
        if not query:
            return None
        try:
            payload = await self._client.get_json(
                DEEZER_SEARCH_URL,
                params={"q": query, "limit": 3, "order": "RANKING"},
                timeout=self._timeout,
            )
        except Exception:
            return None
        tracks: list[dict[str, Any]] = (payload or {}).get("data") or []
        for track in tracks:
            album = track.get("album") or {}
            cover_url = album.get("cover_medium") or album.get("cover")
            if cover_url:
                return await download_image_or_none(self._client, str(cover_url), timeout=self._timeout)
        return None


__all__ = [
    "CoverProvider",
    "DeezerCoverProvider",
]
