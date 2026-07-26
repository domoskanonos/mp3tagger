"""Metadata enrichment providers.

The :class:`MetadataProvider` ABC lets the ripper swap iTunes for MusicBrainz,
Last.fm, etc. The current default is :class:`ITunesMetadataProvider` which uses
the public iTunes Search API (no API key required).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from radio_ripper.domain.models import EnrichedInfo, ITunesTrackData, MusicBrainzData
from radio_ripper.infra.http import AsyncHttpClient
from radio_ripper.infra.resilience import retry_async

ITUNES_SEARCH_URL = "https://itunes.apple.com/search"


class MetadataProvider(ABC):
    """Enrich track metadata (album, year, artwork) from an external source."""

    @abstractmethod
    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:
        """Return enriched info or ``None`` when no match is found."""

    @abstractmethod
    async def download_image(self, url: str) -> bytes | None:
        """Download cover-art bytes; ``None`` on failure."""


class ITunesMetadataProvider(MetadataProvider):
    """iTunes Search API metadata + cover art provider."""

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
        try:
            payload = await self._client.get_json(
                ITUNES_SEARCH_URL,
                params={"term": query, "limit": 1, "entity": "song", "media": "music"},
                timeout=self._metadata_timeout,
            )
        except Exception:
            return None
        results: list[dict[str, Any]] = (payload or {}).get("results") or []
        if not results:
            return None
        hit = results[0]
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

    async def download_image(self, url: str) -> bytes | None:
        try:
            data = await self._client.get_bytes(url, timeout=self._cover_timeout)
        except Exception:
            return None
        if not data or len(data) < 64:
            return None
        return data

    @staticmethod
    def _upgrade_artwork(url: str) -> str:
        """Bump iTunes thumbnail to a higher resolution URL."""
        return (
            url.replace("100x100bb", "600x600bb")
            .replace("60x60bb", "600x600bb")
            .replace("100x100", "600x600")
            .replace("60x60", "600x600")
        )


class NullMetadataProvider(MetadataProvider):
    """No-op provider — used when enrichment is disabled in the config."""

    async def fetch(self, artist: str, title: str) -> EnrichedInfo | None:
        return None

    async def download_image(self, url: str) -> bytes | None:
        return None


class CoverArtProvider(ABC):
    """Fetch cover art and recording metadata via MusicBrainz / Cover Art Archive."""

    @abstractmethod
    async def fetch_cover_by_recording_id(self, recording_id: str) -> bytes | None:
        """Return front-cover bytes for a recording, or ``None``."""

    @abstractmethod
    async def fetch_recording_data(self, recording_id: str) -> MusicBrainzData | None:
        """Return detailed MusicBrainz metadata for a recording MBID."""


class NullCoverArtProvider(CoverArtProvider):
    """No-op provider — used when CAA is disabled."""

    async def fetch_cover_by_recording_id(self, recording_id: str) -> bytes | None:
        return None

    async def fetch_recording_data(self, recording_id: str) -> MusicBrainzData | None:
        return None


class CoverArtArchiveProvider(CoverArtProvider):
    """Fetch album cover art from coverartarchive.org via a MusicBrainz recording MBID.

    Used as a secondary source when iTunes enrichment returned no artwork.
    The flow is: MBID -> MusicBrainz /ws/2/recording lookup (to get releases)
    -> for each release, fetch its front-cover bytes from coverartarchive.org.
    """

    _MBZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/{mbid}"
    _MBZ_RELEASE_URL = "https://musicbrainz.org/ws/2/release/{release_id}"
    _CAA_RELEASE_FRONT = "https://coverartarchive.org/release/{mbid}/front"
    _USER_AGENT = "Radio-Ripper/2.0 (https://github.com/artokun/radioripper)"
    _MAX_RELEASES_TO_TRY = 5

    async def fetch_cover_by_recording_id(self, recording_id: str) -> bytes | None:
        """Look up the MusicBrainz recording, then fetch front cover bytes.

        Returns ``None`` if *recording_id* is empty, the MBZ lookup fails,
        there are no releases, or none of the cover-art fetches yield bytes.
        """
        if not recording_id:
            return None
        releases = await self._fetch_recording_releases(recording_id)
        if releases is None:
            return None
        for rel in releases[: self._MAX_RELEASES_TO_TRY]:
            mbid = rel.get("id")
            if not mbid:
                continue
            cover = await self.download_image(self._CAA_RELEASE_FRONT.format(mbid=mbid))
            if cover:
                return cover
        return None

    async def fetch_recording_data(self, recording_id: str) -> MusicBrainzData | None:
        """Fetch detailed MusicBrainz metadata for a recording MBID.

        Two-step lookup:
          1. recording → releases + ISRCs + genres
          2. first official release → labels + release-group type

        Returns a :class:`MusicBrainzData` or ``None`` on failure.
        """
        if not recording_id:
            return None
        releases = await self._fetch_recording_releases(
            recording_id,
            extra_inc="releases+isrcs+genres",
        )
        if releases is None:
            return None

        payload = self._recording_cache.get(recording_id, {})

        isrcs: tuple[str, ...] = ()
        with contextlib.suppress(Exception):
            raw = payload.get("isrcs") or []
            isrcs = tuple(r["isrc"] for r in raw if r.get("isrc"))

        genres: tuple[str, ...] = ()
        with contextlib.suppress(Exception):
            genres = tuple(g["name"] for g in (payload.get("genres") or []) if g.get("name"))

        # Pick the first official release (earliest date = original)
        official = [r for r in releases if r.get("status") == "Official"]
        official.sort(key=lambda r: r.get("date") or "")
        chosen = official[0] if official else releases[0] if releases else None
        if chosen is None:
            return MusicBrainzData(recording_id=recording_id, isrcs=isrcs, genres=genres)

        release_payload: dict[str, Any] | None = None
        with contextlib.suppress(Exception):
            release_payload = await self._rate_limited_json(
                self._MBZ_RELEASE_URL.format(release_id=chosen["id"]),
                params={"fmt": "json", "inc": "labels+release-groups"},
            )

        label_name: str | None = None
        catalog_no: str | None = None
        if release_payload:
            with contextlib.suppress(Exception):
                info = (release_payload.get("label-info") or [])[0]
                if info:
                    label_name = info.get("label", {}).get("name")
                    catalog_no = info.get("catalog-number")

        rg_type: str | None = None
        if release_payload:
            with contextlib.suppress(Exception):
                rg = release_payload.get("release-group") or {}
                prim = rg.get("primary-type") or ""
                sec = rg.get("secondary-types") or []
                parts = [prim] + [s for s in sec if s]
                rg_type = " / ".join(parts) if parts else None

        length_ms: int | None = payload.get("length")

        return MusicBrainzData(
            recording_id=recording_id,
            length_ms=length_ms,
            isrcs=isrcs,
            genres=genres,
            release_id=chosen.get("id"),
            release_title=chosen.get("title"),
            release_label=label_name,
            release_catalog_no=catalog_no,
            release_date=chosen.get("date"),
            release_country=chosen.get("country"),
            release_group_type=rg_type,
            barcode=release_payload.get("barcode") if release_payload else None,
        )

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 8.0) -> None:
        self._client = client
        self._timeout = timeout
        self._last_mb_request: float = 0.0
        self._recording_cache: dict[str, dict[str, Any]] = {}

    @retry_async(max_attempts=2, base_delay=0.5, exceptions=(httpx.HTTPError,))
    async def _rate_limited_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """MusicBrainz rate limit: 1 request / second."""
        since_last = time.monotonic() - self._last_mb_request
        if since_last < 1.0:
            await asyncio.sleep(1.0 - since_last)
        self._last_mb_request = time.monotonic()
        try:
            return await self._client.get_json(url, params=params, timeout=self._timeout)
        except Exception:
            return None

    async def _fetch_recording_releases(
        self,
        recording_id: str,
        extra_inc: str = "releases",
    ) -> list[dict[str, Any]] | None:
        """Fetch the recording JSON and return its release list.

        Caches the raw payload in ``self._recording_cache`` so that
        ``fetch_cover_by_recording_id`` and ``fetch_recording_data``
        don't duplicate the network call.
        """
        if recording_id in self._recording_cache:
            return (self._recording_cache[recording_id] or {}).get("releases") or []
        payload = await self._rate_limited_json(
            self._MBZ_RECORDING_URL.format(mbid=recording_id),
            params={"fmt": "json", "inc": extra_inc},
        )
        self._recording_cache[recording_id] = payload or {}
        return ((payload or {}).get("releases") or []) or None

    async def download_image(self, url: str) -> bytes | None:
        try:
            data = await self._client.get_bytes(url, timeout=self._timeout)
        except Exception:
            return None
        if not data or len(data) < 64:
            return None
        return data


__all__ = [
    "CoverArtArchiveProvider",
    "CoverArtProvider",
    "ITunesMetadataProvider",
    "MetadataProvider",
    "NullCoverArtProvider",
    "NullMetadataProvider",
]
