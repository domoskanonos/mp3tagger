"""Deezer-Metadaten-Provider — Cover, Label, Genre, Album, Jahr, Dauer, ISRC.

:class:`DeezerMetadataProvider` sucht über die öffentliche Deezer Search API
(kein API-Key), holt optional Album-Detail (Label, Genre, Release-Datum).

Priority nach Trefferwahrscheinlichkeit:
  - Cover:  95 %+
  - Label:  90 %+  (album.label)
  - Genre:  95 %   (genres.data direkt von Deezer)
  - Album:  95 %+  (album.title)
  - Jahr:   95 %   (release_date)
  - ISRC:   95 %+
  - Dauer:  95 %   (duration in Sekunden)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from radio_ripper.infra.http import AsyncHttpClient, download_image_or_none

_log = logging.getLogger(__name__)

DEEZER_SEARCH_URL = "https://api.deezer.com/search"


@dataclass
class DeezerData:
    cover_bytes: bytes | None = None
    album: str | None = None
    label: str | None = None
    genre: str | None = None
    year: str | None = None
    duration_s: int | None = None
    isrc: str | None = None
    upc: str | None = None
    rank: int | None = None


class DeezerProvider(ABC):
    @abstractmethod
    async def fetch(
        self, artist: str, title: str, *, fetch_album_detail: bool = True
    ) -> DeezerData | None:
        ...


class DeezerMetadataProvider(DeezerProvider):
    """Deezer Search + optional Album-Detail."""

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 8.0) -> None:
        self._client = client
        self._timeout = timeout

    async def fetch(
        self, artist: str, title: str, *, fetch_album_detail: bool = True
    ) -> DeezerData | None:
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
            _log.debug("Deezer search failed: %s / %s", artist, title)
            return None
        tracks: list[dict[str, Any]] = (payload or {}).get("data") or []
        if not tracks:
            return None
        track = tracks[0]
        album_data = track.get("album") or {}

        cover_url = (
            album_data.get("cover_xl")
            or album_data.get("cover_big")
            or album_data.get("cover_medium")
        )
        cover_bytes = (
            await download_image_or_none(self._client, str(cover_url), timeout=self._timeout)
            if cover_url
            else None
        )

        result = DeezerData(
            cover_bytes=cover_bytes,
            album=album_data.get("title"),
            duration_s=track.get("duration"),
            isrc=track.get("isrc"),
            rank=track.get("rank"),
        )

        album_id = album_data.get("id")
        if fetch_album_detail and album_id:
            try:
                detail = await self._client.get_json(
                    f"https://api.deezer.com/album/{album_id}",
                    timeout=self._timeout,
                )
            except Exception:
                detail = None

            if detail:
                result.label = detail.get("label") or None
                result.upc = detail.get("upc") or None
                rdate = detail.get("release_date") or ""
                if len(rdate) >= 4:
                    result.year = rdate[:4]

                genres_data = (detail.get("genres") or {}).get("data") or []
                for g in genres_data:
                    gname = g.get("name")
                    if gname and gname != "Alle":
                        result.genre = gname
                        break

                if not result.album and detail.get("title"):
                    result.album = detail.get("title")

        return result


__all__ = [
    "DeezerData",
    "DeezerMetadataProvider",
    "DeezerProvider",
]
