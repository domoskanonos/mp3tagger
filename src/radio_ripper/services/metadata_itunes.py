"""iTunes-Metadaten-Anreicherung.

:class:`ITunesMetadataProvider` sucht über die öffentliche iTunes Search API
nach Künstler/Titel und liefert Album, Jahr, Genre, Cover-URL und iTunes-IDs.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from typing import Any

import httpx

from radio_ripper.domain.models import EnrichedInfo, ITunesTrackData
from radio_ripper.infra.http import AsyncHttpClient, download_image_or_none
from radio_ripper.infra.resilience import retry_async

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"

_PARENS_RE = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_ARTWORK_SIZE_RE = re.compile(r"\d+x\d+(?:bb|cc)?(?=\.(?:jpg|jpeg|png|webp))")


def _strip_parens(text: str) -> str:
    """Entfernt Klammerausdrücke für eine breitere iTunes-Suche.

    iTunes Search ist exakt: "I See a Dark(er)ness" findet nichts,
    "I See a Darkness" schon. Dieser Normalizer wird für die
    Fallback-Suche verwendet, wenn die primäre Suche leer bleibt.
    """
    cleaned = _PARENS_RE.sub("", text)
    return re.sub(r"\s+", " ", cleaned)


class MetadataProvider(ABC):
    """Reichert Track-Metadaten (Album, Jahr, Cover) aus einer externen Quelle an."""

    @abstractmethod
    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:
        """Gibt angereicherte Infos zurück oder ``None`` bei keinem Treffer."""

    @abstractmethod
    async def download_image(self, url: str) -> bytes | None:
        """Cover-Bild herunterladen; ``None`` bei Fehler."""

    async def fetch_artist_image(self, artist: str) -> bytes | None:
        """Künstler-Porträt als Bytes oder ``None`` (Standard: nicht verfügbar)."""
        del artist
        return None


class ITunesMetadataProvider(MetadataProvider):
    """iTunes Search API — Album, Jahr, Genre, iTunes-IDs, Cover-URL.

    Kein API-Key erforderlich. Die Suche erfolgt über den öffentlichen
    iTunes Search-Endpunkt (entity=song, limit=1).
    """

    def __init__(
        self,
        client: AsyncHttpClient,
        *,
        metadata_timeout: float = 8.0,
        cover_timeout: float = 15.0,
    ) -> None:
        self._client = client
        self._metadata_timeout = metadata_timeout
        self._cover_timeout = cover_timeout

    @retry_async(max_attempts=2, base_delay=0.5, exceptions=(httpx.HTTPError,))
    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:  # type: ignore[override]
        query = f"{artist} {title}".strip()
        if not query:
            return None
        countries = ("DE", "US", "GB")
        # Zusätzliche Query-Varianten: ohne Klammern (z.B. "(Original Mix)"),
        # damit auch dann getroffen wird, wenn der erste Treffer falsch ist.
        queries = [query]
        stripped = _strip_parens(query).strip()
        if stripped and stripped != query:
            queries.append(stripped)
        if title:
            only_title = _strip_parens(title).strip()
            if only_title and f"{artist} {only_title}".strip() != query:
                queries.append(f"{artist} {only_title}".strip())

        hit = None
        for country in countries:
            for q in queries:
                hit = await self._search_one(q, country=country)
                if hit:
                    break
            if hit:
                break
        if hit is None:
            return None
        artwork = hit.get("artworkUrl100") or hit.get("artworkUrl60")
        if artwork:
            artwork = self._upgrade_artwork(artwork)
        itunes_data = ITunesTrackData(
            track_id=hit.get("trackId"),
            artist_id=hit.get("artistId"),
            collection_id=hit.get("collectionId"),
            track_view_url=hit.get("trackViewUrl"),
            preview_url=hit.get("previewUrl"),
            track_count=hit.get("trackCount"),
            disc_count=hit.get("discCount"),
            country=hit.get("country"),
            explicitness=hit.get("collectionExplicitness") or hit.get("trackExplicitness"),
        )
        return EnrichedInfo(
            artist=hit.get("artistName"),
            title=hit.get("trackName"),
            album=hit.get("collectionName"),
            year=(hit.get("releaseDate") or "")[:4] or None,
            genre=hit.get("primaryGenreName"),
            label=hit.get("recordLabel"),
            track_number=hit.get("trackNumber"),
            disc_number=hit.get("discNumber"),
            track_length=hit.get("trackTimeMillis"),
            artwork_url=artwork,
            itunes_data=itunes_data,
        )

    async def _search_one(self, query: str, country: str = "US") -> dict[str, Any] | None:
        """iTunes-Suche im angegebenen Store-Land.

        Holt bis zu 5 Treffer und wählt den ersten mit vorhandener Cover-URL
        (und möglichst passendem Künstlernamen) — damit werden falsche erste
        Treffer übersprungen, die kein Cover liefern würden.
        """
        try:
            payload = await self._client.get_json(
                ITUNES_SEARCH_URL,
                params={"term": query, "limit": 5, "entity": "song", "media": "music", "country": country},
                timeout=self._metadata_timeout,
            )
        except Exception:
            return None
        results: list[dict[str, Any]] = (payload or {}).get("results") or []
        for r in results:
            if r.get("artworkUrl100") or r.get("artworkUrl60"):
                return r
        return results[0] if results else None

    async def fetch_artist_image(self, artist: str) -> bytes | None:
        """Künstler-Porträt über die iTunes Artist-Suche (entity=musicArtist)."""
        if not artist:
            return None
        try:
            payload = await self._client.get_json(
                ITUNES_SEARCH_URL,
                params={"term": artist, "limit": 1, "entity": "musicArtist", "media": "music", "country": "US"},
                timeout=self._metadata_timeout,
            )
        except Exception:
            return None
        results: list[dict[str, Any]] = (payload or {}).get("results") or []
        if not results:
            return None
        artwork = results[0].get("artworkUrl100") or results[0].get("artworkUrl60")
        if not artwork:
            return None
        return await self.download_image(self._upgrade_artwork(artwork))

    async def download_image(self, url: str) -> bytes | None:
        return await download_image_or_none(self._client, url, timeout=self._cover_timeout)

    @staticmethod
    def _upgrade_artwork(url: str) -> str:
        """Erhöht die iTunes-Thumbnail-Auflösung auf 600px.

        Behandelt beliebige Größen-Token wie ``100x100bb``, ``100x100cc``,
        ``60x60bb``, ``55x55`` etc. — nicht nur feste Varianten.
        """
        return _ARTWORK_SIZE_RE.sub("600x600bb", url)


__all__ = [
    "ITunesMetadataProvider",
    "MetadataProvider",
]
