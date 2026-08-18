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

# Dauer-Abweichung wird nur noch als Sortier-Präferenz genutzt (siehe
# ``_pick_best_candidate``) — kein harter Ausschluss mehr.

# Der AcoustID-Server filtert Kandidaten hart anhand des `duration`-Parameters:
# Der Match-Algorithmus sucht nur in einem schmalen Dauer-Band um das Recording,
# und Radio-Rips sind fast immer LÄNGER als das Original (Jingle, Moderation,
# Talkover am Anfang/Ende). Deshalb wird zusätzlich zur vollen Dateilänge eine
# absteigende Reihe von `duration`-Werten probiert (siehe ``_candidate_durations``).
# Der erste Dauerwert, dessen bester Kandidat akzeptabel ist (Score >= min),
# gewinnt; ein reiner „irgendein Treffer“ mit zu niedrigem Score stoppt die Suche
# NICHT, weil ein anderer Dauerwert oft deutlich besser matcht.
_DURATION_RETRY_STEPS_S = (2, 4, 6, 8, 10, 15, 20, 25, 30, 40, 50, 60, 80, 100, 120)
# Untergrenze für probierte Dauerwerte — kürzere Werte sind für reale Songs
# unsinnig und sparen nur API-Calls.
_DURATION_RETRY_MIN_S = 30

# AcoustID's API antwortet bei Throttle mit status="error" (HTTP 429, error.code
# meist 9). Permanente Fehler (ungültiger Key code 4, fehlender/ungültiger
# Parameter code 2/3) retten auch kein Warten — die Datei wird dann via
# FingerprintError zur späteren Verarbeitung aufbewahrt, aber nicht 10s lange
# blockiert.
_THROTTLE_WAIT_S = 10.0
_THROTTLE_MAX_ATTEMPTS = 3
# Error-Codes der AcoustID-API (Webservice-Doku): 4 = invalid API key,
# 2 = missing required parameter, 3 = invalid parameter value/fingerprint.
# Diese Fehler sind permanent — Retry bringt nichts.
_PERMANENT_ERROR_CODES = frozenset({2, 3, 4})


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


def _join_artist_names(artists: list[Any]) -> str | None:
    """Fügt die Artist-Namen inkl. Join-Phrases zu einem String zusammen.

    Mit ``usermeta`` liefert AcoustID ``artists`` teils als Liste von Strings
    statt Dicts — beides wird unterstützt.
    """
    if not artists:
        return None
    parts: list[str] = []
    for a in artists:
        if isinstance(a, dict):
            parts.append(str(a.get("name", "")))
            parts.append(str(a.get("joinphrase", "")))
        else:
            parts.append(str(a))
    return "".join(parts).strip() or None


def _artist_mbid(artists: list[Any]) -> str | None:
    """Erste Artist-MBID, falls die Artists als Dict-Liste vorliegen."""
    if not artists:
        return None
    first = artists[0]
    if isinstance(first, dict):
        return str(first.get("id"))
    return None


def _pick_best_candidate(
    results: list[dict[str, Any]],
    real_duration: float | None,
) -> tuple[float, str, str, str, str | None] | None:
    """Wählt den besten AcoustID-Kandidaten.

    Bewertung über alle Recordings aller Ergebnisse (nur Ranking, KEINE
    harten Ausschlusskriterien — der Server hat bereits anhand der ``duration``
    gefiltert, alle zurückgegebenen Recordings sind echte Treffer):
      1. Primär: kleinste |duration - real_duration| (Tiebreaker, kein Reject —
         Radio-Rips sind oft länger, gekürzte Rips kürzer als das Original)
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
            artist_mbid = _artist_mbid(artists)
            if not recording_id and not title:
                continue
            # Ohne Dauer-Abgleich als Fallback-Kandidat aufnehmen (schlechter bewertet)
            delta = float("inf") if duration is None or real_duration is None else abs(float(duration) - real_duration)
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
                # Fingerprint nur einmal erzeugen; der Lookup läuft mit mehreren
                # Dauerwerten (siehe _candidate_durations).
                full_duration, fp = await loop.run_in_executor(None, acoustid.fingerprint_file, str(path))
        except acoustid.NoBackendError as exc:
            raise FingerprintError(f"acoustid backend unavailable: {exc}") from exc
        except acoustid.FingerprintGenerationError as exc:
            raise NonRetriableFingerprintError(str(exc)) from exc
        except Exception as exc:
            raise FingerprintError(f"acoustid fingerprint failed: {exc}") from exc

        # parse=False liefert die volle JSON-Antwort (recordings, duration,
        # sources, releasegroups, artists) — nötig für die Kandidaten-Auswahl.
        # usermeta sorgt dafür, dass das recordings-Array zuverlässiger gefüllt
        # wird (sonst liefert AcoustID oft nur {id, score} ohne recordings).
        meta = ["recordings", "releases", "releasegroups", "sources", "usermeta"]
        best: tuple[float, str, str, str, str | None] | None = None
        best_duration: int | None = None
        for duration_s in self._candidate_durations(full_duration):
            payload = await self._lookup_with_throttle_retry(loop, acoustid, fp, duration_s, meta, path)

            found = payload.get("results") if isinstance(payload, dict) else None
            if not found:
                continue
            candidate = _pick_best_candidate(found, real_duration)
            if candidate is None:
                continue
            cand_score, _cand_rid, cand_artist, cand_title, _ = candidate
            if cand_score >= self._min_score and (cand_artist or cand_title):
                # Erster akzeptabler Kandidat gewinnt — spart API-Calls.
                # Eine leere recording_id ist akzeptabel: AcoustID liefert mit
                # usermeta teils nur user-submitted Einträge ohne MBID.
                best = candidate
                best_duration = duration_s
                break
            # Noch kein akzeptabler Kandidat: besseren Zwischenstand merken,
            # damit ein späterer Dauerwert (oft deutlich besser) weitersuchen darf.
            if best is None or cand_score > best[0]:
                best = candidate
                best_duration = duration_s

        if best is None:
            return None
        score, recording_id, artist, title, artist_mbid = best
        if score < self._min_score:
            return None
        if not artist and not title:
            return None
        if best_duration is not None and best_duration != int(full_duration):
            _log.info(
                "AcoustID: duration %ss lieferte den Treffer (volle Dateilänge %ss)",
                best_duration,
                int(full_duration),
            )
        return FingerprintResult(
            artist=artist,
            title=title,
            score=score,
            recording_id=recording_id,
            artist_mbid=artist_mbid,
        )

    @classmethod
    def _candidate_durations(cls, full_duration: float) -> list[int]:
        """Liefert eine absteigende Liste von ``duration``-Werten für den Lookup.

        Der AcoustID-Server verwirft Treffer, wenn die gesendete ``duration``
        von der Dauer des DB-Recordings abweicht (oft schon ±1-2s). Radio-Rips
        sind häufig länger als das Original (Jingle/Moderation am Anfang/Ende),
        daher wird zuerst die volle Dateilänge probiert und dann eine absteigende
        Reihe, bis ein Treffer kommt. Rein deterministisch und sortiert, damit
        die ersten Werte die wahrscheinlichsten sind.
        """
        full = int(full_duration)
        candidates = [full]
        for step in _DURATION_RETRY_STEPS_S:
            candidate = full - step
            if candidate < _DURATION_RETRY_MIN_S:
                break
            candidates.append(candidate)
        return candidates

    async def _lookup_with_throttle_retry(
        self,
        loop: asyncio.AbstractEventLoop,
        acoustid: Any,
        fp: bytes,
        duration_s: int,
        meta: list[str],
        path: Path,
    ) -> dict[str, Any]:
        """Führt einen Lookup aus, behandelt API-Fehler und Throttle.

        pyacoustid ruft **nie** ``raise_for_status()`` auf: Bei HTTP-Fehlern
        (429 Throttle, 500, 400) liefert die API ein JSON mit ``status:
        "error"`` — ein normales Payload ohne ``results``. Diese Antworten
        dürfen NICHT als "kein Treffer" (None) durchgereicht werden, sonst
        löscht der Caller die Datei. Stattdessen:

        - ``status != "ok"`` bei einem PERMANENTEN Fehler (ungültiger Key,
          fehlender/ungültiger Parameter) → sofort :class:`FingerprintError`
          (Datei wird NICHT gelöscht, sondern für späteren Retry aufbewahrt).
        - ``status != "ok"`` bei transientem Fehler (Throttle/429, 5xx) →
          ``_THROTTLE_WAIT_S`` warten und erneut versuchen (max.
          ``_THROTTLE_MAX_ATTEMPTS``), dann :class:`FingerprintError`.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                payload = await loop.run_in_executor(None, acoustid.lookup, self._api_key, fp, duration_s, meta)
            except acoustid.WebServiceError as exc:
                msg = str(exc)
                if "status: error" in msg:
                    raise FingerprintError(f"AcoustID API error (invalid key?): {exc}") from exc
                raise FingerprintError(f"acoustid lookup failed: {exc}") from exc
            except Exception as exc:
                raise FingerprintError(f"acoustid lookup failed: {exc}") from exc

            if not isinstance(payload, dict):
                raise FingerprintError(f"acoustid lookup failed: unexpected payload {payload!r}")
            status = payload.get("status")
            if status == "ok":
                return payload

            # status != "ok" → API-Fehler oder Throttle (kein "kein Treffer"!).
            error_raw = payload.get("error")
            error = error_raw if isinstance(error_raw, dict) else {}
            error_code = error.get("code")
            if error_code in _PERMANENT_ERROR_CODES:
                _log.warning(
                    "AcoustID permanenter API-Fehler für %s (code=%r, status=%r) — "
                    "Datei wird NICHT verworfen, für späteren Retry aufbewahrt: %s",
                    path.name,
                    error_code,
                    status,
                    payload,
                )
                raise FingerprintError(f"AcoustID API error: code={error_code!r}")

            if attempt >= _THROTTLE_MAX_ATTEMPTS:
                _log.warning(
                    "AcoustID API error for %s (status=%r, attempt %d/%d) — Datei wird NICHT verworfen: %s",
                    path.name,
                    status,
                    attempt,
                    _THROTTLE_MAX_ATTEMPTS,
                    payload,
                )
                raise FingerprintError(f"AcoustID API error: status={status!r}")
            _log.warning(
                "AcoustID API error (status=%r) für %s — warte %ss und versuche erneut (%d/%d)",
                status,
                path.name,
                _THROTTLE_WAIT_S,
                attempt,
                _THROTTLE_MAX_ATTEMPTS,
            )
            await asyncio.sleep(_THROTTLE_WAIT_S)

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
