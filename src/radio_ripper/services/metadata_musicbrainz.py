"""MusicBrainz / Cover Art Archive — Album-Cover und MB-Metadaten.

:class:`CoverArtArchiveProvider` löst eine AcoustID-Recording-MBID in
Album-Artwork und detaillierte MusicBrainz-Metadaten (Label, Katalog-Nr.,
ISRCs, Genres) auf. Die MB-API wird ratelimited (1 Request/Sekunde).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from radio_ripper.domain.models import MusicBrainzData
from radio_ripper.infra.http import AsyncHttpClient
from radio_ripper.infra.resilience import retry_async


async def _fetch_image(client: AsyncHttpClient, url: str, timeout: float) -> bytes | None:
    """Lädt ein Bild von einer URL herunter, gibt ``None`` bei Fehler oder zu kleinen Daten."""
    try:
        data = await client.get_bytes(url, timeout=timeout)
    except Exception:
        return None
    if not data or len(data) < 64:
        return None
    return data


class CoverArtProvider(ABC):
    """Cover-Art und Recording-Metadaten via MusicBrainz / Cover Art Archive."""

    @abstractmethod
    async def fetch_cover_by_recording_id(self, recording_id: str) -> bytes | None:
        """Front-Cover-Bytes für eine Recording-MBID oder ``None``."""

    @abstractmethod
    async def fetch_recording_data(self, recording_id: str) -> MusicBrainzData | None:
        """Detaillierte MusicBrainz-Metadaten für eine Recording-MBID."""


class CoverArtArchiveProvider(CoverArtProvider):
    """Ruft Album-Artwork von coverartarchive.org über eine MusicBrainz-Recording-MBID ab.

    Ablauf: MBID → MusicBrainz /ws/2/recording (Releases ermitteln)
            → für jedes Release Front-Cover von coverartarchive.org holen
            → erstes erfolgreiches Cover zurückgeben.
    """

    _MBZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/{mbid}"
    _MBZ_RELEASE_URL = "https://musicbrainz.org/ws/2/release/{release_id}"
    _CAA_RELEASE_FRONT = "https://coverartarchive.org/release/{mbid}/front"
    _USER_AGENT = "Radio-Ripper/2.0 (https://github.com/artokun/radioripper)"
    _MAX_RELEASES_TO_TRY = 5

    def __init__(self, client: AsyncHttpClient, *, timeout: float = 8.0) -> None:
        self._client = client
        self._timeout = timeout
        self._last_mb_request: float = 0.0
        self._recording_cache: dict[str, dict[str, Any]] = {}

    async def fetch_cover_by_recording_id(self, recording_id: str) -> bytes | None:
        """Ermittelt Releases zur MBID und versucht, das Front-Cover abzurufen."""
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
        """Zweistufiger MB-Lookup: Recording → Releases → erstes offizielles Release.

        Liefert Recording-Titel, -Künstler, ISRCs, Genres sowie
        Release-Informationen (Label, Katalog-Nr., Datum, Land, Barcode).
        """
        if not recording_id:
            return None
        releases = await self._fetch_recording_releases(recording_id)
        if releases is None:
            return None

        payload = self._recording_cache.get(recording_id, {})

        # Künstler aus artist-credit parsen
        recording_title: str | None = payload.get("title")
        recording_artist: str | None = None
        with contextlib.suppress(Exception):
            credits = payload.get("artist-credit") or []
            parts: list[str] = []
            for c in credits:
                if isinstance(c, dict):
                    parts.append(c.get("name", ""))
                    parts.append(c.get("joinphrase", ""))
                elif isinstance(c, str):
                    parts.append(c)
            recording_artist = "".join(parts).strip() or None

        isrcs: tuple[str, ...] = ()
        with contextlib.suppress(Exception):
            raw = payload.get("isrcs") or []
            isrcs = tuple(r["isrc"] for r in raw if r.get("isrc"))

        genres: tuple[str, ...] = ()
        with contextlib.suppress(Exception):
            genres = tuple(g["name"] for g in (payload.get("genres") or []) if g.get("name"))

        # Frühestes offizielles Release wählen
        official = [r for r in releases if r.get("status") == "Official"]
        official.sort(key=lambda r: r.get("date") or "")
        chosen = official[0] if official else releases[0] if releases else None
        if chosen is None:
            return MusicBrainzData(recording_id=recording_id, isrcs=isrcs, genres=genres)

        # Release-Details (Labels, Release-Group) abrufen
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
            recording_title=recording_title,
            recording_artist=recording_artist,
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

    @retry_async(max_attempts=2, base_delay=0.5, exceptions=(httpx.HTTPError,))
    async def _rate_limited_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """MusicBrainz-Rate-Limit: maximal 1 Request pro Sekunde."""
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
        extra_inc: str = "artists+releases+isrcs+genres",
    ) -> list[dict[str, Any]] | None:
        """Ruft das Recording-JSON ab und gibt die Release-Liste zurück.

        Die Rohdaten werden in ``self._recording_cache`` zwischengespeichert,
        sodass ``fetch_cover_by_recording_id`` und ``fetch_recording_data``
        sich den Netzwerkaufruf teilen.
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
        return await _fetch_image(self._client, url, self._timeout)


__all__ = [
    "CoverArtArchiveProvider",
    "CoverArtProvider",
]
