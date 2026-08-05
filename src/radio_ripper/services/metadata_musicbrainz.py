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
from radio_ripper.infra.http import AsyncHttpClient, download_image_or_none
from radio_ripper.infra.resilience import retry_async


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
    _CAA_RELEASE_FRONT = "https://coverartarchive.org/release/{mbid}/front"
    _USER_AGENT = "Radio-Ripper/2.0 (https://github.com/artokun/radioripper)"
    _MAX_RELEASES_TO_TRY = 5

    def __init__(
        self,
        client: AsyncHttpClient,
        *,
        timeout: float = 8.0,
        cover_timeout: float | None = None,
    ) -> None:
        self._client = client
        self._timeout = timeout
        self._cover_timeout = cover_timeout if cover_timeout is not None else timeout
        self._last_mb_request: float = 0.0
        self._recording_cache: dict[str, dict[str, Any]] = {}
        # Serialisiert MB-Zugriffe: Rate-Limit (1 req/s) UND Cache-Schreibzugriffe
        # müssen atomar sein — sonst feuern parallele Tasks (max_concurrent) den
        # MB-Endpunkt im Burst und das Rate-Limit (429) verschluckt Covers.
        self._mb_lock = asyncio.Lock()

    async def fetch_cover_by_recording_id(self, recording_id: str) -> bytes | None:
        """Ermittelt Releases zur MBID und versucht, das Front-Cover abzurufen.

        Transiente MB-HTTP-Fehler werden intern retried (_rate_limited_json);
        bleibt ein Fehler bestehen, wird ``None`` zurückgegeben statt zu crashen.
        """
        if not recording_id:
            return None
        try:
            releases = await self._fetch_recording_releases(recording_id)
        except Exception:
            return None
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

        Transiente MB-HTTP-Fehler werden intern retried (_rate_limited_json);
        bleibt ein Fehler bestehen, wird ``None`` zurückgegeben statt zu crashen.
        """
        if not recording_id:
            return None
        try:
            releases = await self._fetch_recording_releases(recording_id)
        except Exception:
            return None
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
            if not genres:
                tags = [t["name"] for t in (payload.get("tags") or []) if t.get("name")]
                ignored = {"seen_live", "bootleg", "live", "bootlegs", "live recordings"}
                genres = tuple(t for t in tags if t not in ignored)

        # Frühestes offizielles Release wählen
        official = [r for r in releases if r.get("status") == "Official"]
        official.sort(key=lambda r: r.get("date") or "")
        chosen = official[0] if official else releases[0] if releases else None
        if chosen is None:
            return MusicBrainzData(recording_id=recording_id, isrcs=isrcs, genres=genres)

        # Hinweis: Es wird bewusst KEIN separater Release-Detail-Call gemacht —
        # nur die Daten aus dem Recording-Call (chosen) werden verwendet. Dadurch
        # ist pro Datei genau 1 MB-Call nötig (MB-Rate-Limit 1 req/s bleibt sicher).
        # Label/TPUB kommt über iTunes/Deezer; CatalogNumber/ReleaseGroupType/Barcode
        # entfallen bewusst.

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
            release_date=chosen.get("date"),
            release_country=chosen.get("country"),
        )

    @retry_async(max_attempts=2, base_delay=0.5, exceptions=(httpx.HTTPError,))
    async def _rate_limited_json(
        self,
        url: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """MusicBrainz-Rate-Limit: maximal 1 Request pro Sekunde.

        Das gesamte Rate-Limit (prüfen → sleep → timestamp setzen → request) läuft
        unter einem Lock, damit parallele Tasks nicht gleichzeitig durchschlüpfen
        und MB mit 429 antwortet (sonst gehen Covers im Batch verloren).

        Wichtig: HTTP-Fehler werden NICHT verschluckt — der :func:`retry_async`
        Decorator muss die Exception sehen, um bei transienten MB-Fehlern
        (429/503/Timeout) tatsächlich zu retryen. Nur so gehen Covers unter Last
        nicht verloren. Aufrufer (z.B. ``_fetch_cover_data``) fangen den Fehler
        nach dem letzten Versuch ab und geben ``None`` zurück.

        Der Lock schützt nur das Rate-Limit-Timing (prüfen → sleep →
        Zeitstempel setzen), NICHT den HTTP-Call selbst: Ein einzelner
        langsamer MB-Request blockiert dann nicht mehr alle anderen parallelen
        Tasks, sondern verzögert nur seinen eigenen. Das 1-Request-pro-Sekunde
        Limit bleibt gewahrt, weil jeder Task vor seinem Call ``_last_mb_request``
        prüft und entsprechend schläft.
        """
        async with self._mb_lock:
            since_last = time.monotonic() - self._last_mb_request
            if since_last < 1.0:
                await asyncio.sleep(1.0 - since_last)
            self._last_mb_request = time.monotonic()
        return await self._client.get_json(url, params=params, timeout=self._timeout)

    async def _fetch_recording_releases(
        self,
        recording_id: str,
        extra_inc: str = "artists+releases+isrcs+genres+tags",
    ) -> list[dict[str, Any]] | None:
        """Ruft das Recording-JSON ab und gibt die Release-Liste zurück.

        Die Rohdaten werden in ``self._recording_cache`` zwischengespeichert,
        sodass ``fetch_cover_by_recording_id`` und ``fetch_recording_data``
        sich den Netzwerkaufruf teilen. Cache-Schreibzugriffe laufen unter
        dem Rate-Limit-Lock (parallel-sicher); der Request selbst nimmt den
        Lock intern via _rate_limited_json (kein Doppel-Lock / Deadlock).
        """
        async with self._mb_lock:
            if recording_id in self._recording_cache:
                return (self._recording_cache[recording_id] or {}).get("releases") or []
        payload = await self._rate_limited_json(
            self._MBZ_RECORDING_URL.format(mbid=recording_id),
            params={"fmt": "json", "inc": extra_inc},
        )
        async with self._mb_lock:
            self._recording_cache[recording_id] = payload or {}
        return ((payload or {}).get("releases") or []) or None

    async def download_image(self, url: str) -> bytes | None:
        return await download_image_or_none(self._client, url, timeout=self._cover_timeout)


__all__ = [
    "CoverArtArchiveProvider",
    "CoverArtProvider",
]
