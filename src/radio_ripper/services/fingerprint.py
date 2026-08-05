"""Audio fingerprinting providers (AcoustID / MusicBrainz).

The :class:`FingerprintProvider` ABC lets the ripper identify recorded files
against the AcoustID database. The default implementation uses ``pyacoustid``
which wraps the Chromaprint library.

A :class:`FingerprintError` is raised when fingerprinting fails for
*infrastructure* reasons (missing library, network error, API error, rate
limit). A return value of ``None`` from :meth:`fingerprint` strictly means
"the file was processed successfully but no match was found in the AcoustID
database" — callers may safely discard such files.
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from radio_ripper.domain.models import FingerprintResult

_log = logging.getLogger(__name__)

# Toleranz (Sekunden) für den Duration-Abgleich zwischen Audio-Datei und
# AcoustID-Kandidat. Kandidaten außerhalb dieser Toleranz werden verworfen.
_DURATION_TOLERANCE_S = 10.0


class FingerprintError(RuntimeError):
    """Raised when fingerprinting fails for infrastructure reasons.

    This is distinct from a successful lookup that yields no match, which
    is signalled by :meth:`FingerprintProvider.fingerprint` returning
    ``None``.  Callers MUST NOT discard files when a :class:`FingerprintError`
    is raised — the failure is transient and the file should be retried
    later (the file is kept as ``.untested.mp3`` for retry on next restart).
    """


class NonRetriableFingerprintError(FingerprintError):
    """File is corrupt or undecodable — retrying won't help.

    Raised when AcoustID's ``FingerprintGenerationError`` indicates the file
    cannot be decoded (missing decoder backend, 0 audio channels, corrupt
    data).  Callers SHOULD delete the file and its DB record immediately.
    """


class FingerprintProvider(ABC):
    """Identify a recorded audio file against the AcoustID database."""

    @abstractmethod
    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        """Return :class:`FingerprintResult` if the file matches a known recording.

        Raises:
            FingerprintError: if fingerprinting fails for infrastructure
                reasons (missing library, network error, API error).  A
                return value of ``None`` strictly means "successfully
                looked up but no match found".
        """


def _join_artist_names(artists: list[dict[str, Any]]) -> str | None:
    """Fügt die Artist-Namen inkl. Join-Phrases zu einem String zusammen."""
    if not artists:
        return None
    parts: list[str] = []
    for a in artists:
        parts.append(str(a.get("name", "")))
        parts.append(str(a.get("joinphrase", "")))
    return "".join(parts).strip() or None


def _pick_best_candidate(
    results: list[dict[str, Any]],
    real_duration: float | None,
) -> tuple[float, str, str, str, str | None] | None:
    """Wählt den besten AcoustID-Kandidaten.

    Bewertung über alle Recordings aller Ergebnisse:
      1. Primär: kleinste |duration - real_duration| (harter Filter via Toleranz)
      2. Sekundär: höchstes ``sources``
      3. Tiebreaker: höchster ``score``

    Returns ``(score, recording_id, artist, title, artist_mbid)`` oder ``None``.
    """
    candidates: list[tuple[float, int, float, str, str, str, str | None]] = []
    for result in results:
        score = float(result.get("score") or 0.0)
        for recording in result.get("recordings") or []:
            if not isinstance(recording, dict):
                continue
            title = str(recording.get("title") or "")
            recording_id = str(recording.get("id") or "")
            duration = recording.get("duration")
            sources = int(recording.get("sources") or 0)
            artists = recording.get("artists") or []
            artist_name = _join_artist_names(artists)
            artist_mbid = str(artists[0].get("id")) if artists else None
            if not recording_id and not title:
                continue
            if duration is None or real_duration is None:
                # Ohne Dauer-Abgleich als Fallback-Kandidat aufnehmen (schlechter bewertet)
                delta = float("inf")
            else:
                delta = abs(float(duration) - real_duration)
                if delta > _DURATION_TOLERANCE_S:
                    continue
            candidates.append((delta, sources, score, recording_id, title, artist_name or "", artist_mbid))

    if not candidates:
        return None
    # sortiert: 1. kleinste delta, 2. höchste sources, 3. höchster score
    candidates.sort(key=lambda c: (c[0], -c[1], -c[2]))
    _, _, score, recording_id, title, artist_name, artist_mbid = candidates[0]
    return score, recording_id, artist_name, title, artist_mbid


class AcoustidFingerprintProvider(FingerprintProvider):
    """AcoustID-backed fingerprint provider.

    Args:
        api_key: AcoustID API key.
        min_score: Minimum confidence score (0.0-1.0) to accept a match.
    """

    def __init__(self, api_key: str, *, min_score: float = 0.8) -> None:
        self._api_key = api_key
        self._min_score = min_score

    async def fingerprint(self, path: Path) -> FingerprintResult | None:
        try:
            import acoustid
        except ImportError as exc:
            raise FingerprintError(
                "acoustid library not installed (pip install pyacoustid + system chromaprint)"
            ) from exc

        real_duration = self._read_duration(path)
        loop = asyncio.get_running_loop()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                # parse=False liefert die volle JSON-Antwort (recordings, duration,
                # sources, releasegroups, artists) — nötig für die Kandidaten-Auswahl.
                meta = ["recordings", "releases", "releasegroups", "sources"]
                payload = await loop.run_in_executor(None, acoustid.match, self._api_key, str(path), meta, False)
        except acoustid.WebServiceError as exc:
            msg = str(exc)
            if "status: error" in msg:
                raise FingerprintError(f"AcoustID API error (invalid key?): {exc}") from exc
            raise FingerprintError(f"acoustid lookup failed: {exc}") from exc
        except acoustid.FingerprintGenerationError as exc:
            raise NonRetriableFingerprintError(str(exc)) from exc
        except Exception as exc:
            raise FingerprintError(f"acoustid lookup failed: {exc}") from exc

        results = payload.get("results") if isinstance(payload, dict) else None
        if not results:
            return None
        best = _pick_best_candidate(results, real_duration)
        if best is None:
            return None
        score, recording_id, artist, title, artist_mbid = best
        if score < self._min_score:
            return None
        if not artist and not title:
            return None
        return FingerprintResult(
            artist=artist,
            title=title,
            score=score,
            recording_id=recording_id,
            artist_mbid=artist_mbid,
        )

    @staticmethod
    def _read_duration(path: Path) -> float | None:
        """Liest die tatsächliche Audio-Dauer (Sekunden) via mutagen."""
        try:
            from mutagen.mp3 import MP3

            info = MP3(str(path)).info
            return float(info.length) if info and info.length else None
        except Exception:
            return None


__all__ = [
    "AcoustidFingerprintProvider",
    "FingerprintError",
    "FingerprintProvider",
    "NonRetriableFingerprintError",
]
