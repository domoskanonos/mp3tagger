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

from radio_ripper.domain.models import FingerprintResult

_log = logging.getLogger(__name__)


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
        loop = asyncio.get_running_loop()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                gen = await loop.run_in_executor(None, acoustid.match, self._api_key, str(path))
                results = list(gen)
        except acoustid.WebServiceError as exc:
            msg = str(exc)
            if "status: error" in msg:
                raise FingerprintError(f"AcoustID API error (invalid key?): {exc}") from exc
            raise FingerprintError(f"acoustid lookup failed: {exc}") from exc
        except acoustid.FingerprintGenerationError as exc:
            raise NonRetriableFingerprintError(str(exc)) from exc
        except Exception as exc:
            raise FingerprintError(f"acoustid lookup failed: {exc}") from exc
        if not results:
            return None
        best = results[0]
        score = float(best[0])
        if score < self._min_score:
            return None
        recording_id = str(best[1]) if best[1] else ""
        artist = str(best[2]) if best[2] else ""
        title = str(best[3]) if best[3] else ""
        if not artist and not title:
            return None
        return FingerprintResult(
            artist=artist,
            title=title,
            score=score,
            recording_id=recording_id,
        )


__all__ = [
    "AcoustidFingerprintProvider",
    "FingerprintError",
    "FingerprintProvider",
    "NonRetriableFingerprintError",
]
