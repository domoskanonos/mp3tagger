"""Collection-Management-Logik — pure Funktionen ohne Nebeneffekte.

Hier leben die Entscheidungen:
  * Sind zwei MP3s dieselbe Version?           → :func:`is_same_version`
  * Welche Version ist qualitativ besser?     → :func:`is_better_version`
  * Welcher Song wird bei vollem Sammlungslimit verdrängt? → :func:`pick_eviction_candidate`

Alle Funktionen sind deterministisch und tokenseitig frei (keine I/O, keine
Nebenwirkungen) — einfach zu testen und in der Pipeline einsetzbar.
"""

from __future__ import annotations

from radio_ripper.infra.catalog import SongRecord

# ── Versions-Vergleich ──────────────────────────────────────────────────────


def is_same_version(
    new_recording_id: str | None,
    new_isrc: str | None,
    old_recording_id: str | None,
    old_isrc: str | None,
) -> bool:
    """Entscheidet, ob zwei MP3s dieselbe Version desselben Lieds sind.

    Regel (nur-ISRC):
      1. Gleiche ``recording_id`` (MBID) — sonst definitiv andere Songs.
      2. Beide haben ISRC und die ISRCs stimmen überein.

    Ohne ISRC → ``False`` (beide Versionen bleiben, kein Bitrate-Tausch).
    """
    if new_recording_id is None or old_recording_id is None:
        return False
    if new_recording_id != old_recording_id:
        return False
    if not new_isrc or not old_isrc:
        return False
    return new_isrc == old_isrc


def is_better_version(
    new_score: float | None,
    new_bitrate: int | None,
    new_sample_rate: int | None,
    old_score: float | None,
    old_bitrate: int | None,
    old_sample_rate: int | None,
) -> bool:
    """Entscheidet, ob *new* die bessere Version ist als *old*.

    Feste Priorität (nicht konfigurierbar):
      1. AcoustID-Score (höher = besser, ``None`` wie 0)
      2. Bitrate (höher = besser, ``None`` wie 0)
      3. Sample-Rate (höher = besser, ``None`` wie 0)

    ``True`` heißt: *new* gewinnt, *old* kann gelöscht werden.
    """
    ns = new_score or 0.0
    os_ = old_score or 0.0
    if ns != os_:
        return ns > os_
    nb = new_bitrate or 0
    ob = old_bitrate or 0
    if nb != ob:
        return nb > ob
    return (new_sample_rate or 0) > (old_sample_rate or 0)


# ── Eviction ────────────────────────────────────────────────────────────────


def pick_eviction_candidate(
    candidates: list[SongRecord],
    new_rank: int | None,
) -> SongRecord | None:
    """Findet den Verdrängungskandidaten — Song mit niedrigerem Rank als *new_rank*.

    ``candidates`` wird aufsteigend nach ``popularity_rank`` sortiert.
    Der erste Song mit ``popularity_rank IS NOT NULL`` und ``popularity_rank < new_rank``
    wird als Opfer zurückgegeben. ``None`` wenn kein Kandidat:
      * keine Kandidaten übergeben
      * ``new_rank`` ist ``None``
      * alle Kandidaten haben keinen Rank
      * alle Kandidaten haben einen Rank >= ``new_rank``
    """
    if new_rank is None or not candidates:
        return None
    for c in sorted(candidates, key=lambda r: r.popularity_rank if r.popularity_rank is not None else 2**31 - 1):
        if c.popularity_rank is not None and c.popularity_rank < new_rank:
            return c
    return None


__all__ = [
    "is_better_version",
    "is_same_version",
    "pick_eviction_candidate",
]
