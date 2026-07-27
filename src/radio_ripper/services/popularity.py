"""Popularitäts-Prüfung über die öffentliche Deezer-API.

Löscht Tracks, die einen Mindest-Popularitätsrang unterschreiten.
Kein API-Key nötig — Deezer's Such-Endpunkt ist öffentlich.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import httpx

from radio_ripper.infra.http import AsyncHttpClient
from radio_ripper.infra.resilience import retry_async
from radio_ripper.services.file_utils import safe_unlink

_LOGGER = logging.getLogger("radio_ripper.popularity")
_DELAY = 0.2


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


async def maybe_delete_unpopular(
    *,
    file_path: Path,
    station_name: str,
    artist: str,
    title: str,
    min_rank: int,
    popularity_provider: PopularityProvider | None,
    logger: logging.Logger = _LOGGER,
) -> bool:
    """Löscht *file_path*, wenn der Deezer-Rang unter *min_rank* liegt
    oder Deezer den Track nicht kennt (rank=None).

    Die Prüfung ist "best effort" — Fehler werden geloggt, nie weitergereicht.
    Gibt ``True`` zurück, wenn die Datei gelöscht wurde.
    """
    if min_rank <= 0 or popularity_provider is None:
        return False
    if not artist and not title:
        return False

    rank = await popularity_provider.get_rank(artist, title)
    if rank is None:
        safe_unlink(file_path)
        logger.warning(
            "[%s] Deleted unknown track (not on Deezer): %s",
            station_name,
            file_path.name,
        )
        return True

    logger.info(
        "[%s] Popularity rank %s — %s / %s = %d",
        station_name,
        "DELETED" if rank < min_rank else "OK",
        artist,
        title,
        rank,
    )

    if rank >= min_rank:
        return False

    safe_unlink(file_path)

    logger.warning(
        "[%s] Deleted unpopular track (rank=%d < min=%d): %s",
        station_name,
        rank,
        min_rank,
        file_path.name,
    )
    return True


__all__ = [
    "DeezerPopularityChecker",
    "PopularityProvider",
    "maybe_delete_unpopular",
]
