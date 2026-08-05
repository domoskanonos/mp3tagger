"""Tests for radio_ripper.services.fingerprint."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintError,
)


def _payload(recordings: list[dict]) -> dict:
    return {"status": "ok", "results": [{"id": "r1", "score": 0.9, "recordings": recordings}]}


def _recording(
    *,
    title: str,
    artist: str = "Test Artist",
    duration: float = 200.0,
    sources: int = 10,
    rid: str = "rec123",
    artist_mbid: str = "art123",
) -> dict:
    return {
        "id": rid,
        "title": title,
        "duration": duration,
        "sources": sources,
        "artists": [{"id": artist_mbid, "name": artist}],
    }


class TestAcoustidFingerprintProvider:
    async def test_returns_match_for_good_result(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        fake = _payload([_recording(title="Test Title")])

        with patch("acoustid.match", return_value=fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is not None
        assert result.artist == "Test Artist"
        assert result.title == "Test Title"
        assert result.score == 0.9
        assert result.recording_id == "rec123"
        assert result.artist_mbid == "art123"

    async def test_returns_none_when_score_below_threshold(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.95)
        fake = _payload([_recording(title="Test Title")])

        with patch("acoustid.match", return_value=fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is None

    async def test_returns_none_when_no_results(self) -> None:
        provider = AcoustidFingerprintProvider("test-key")
        with patch("acoustid.match", return_value={"status": "ok", "results": []}):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_raises_fingerprint_error_on_acoustid_exception(self) -> None:
        """Infrastructure failures (API down, network) must raise, NOT return None."""
        provider = AcoustidFingerprintProvider("test-key")
        with (
            patch("acoustid.match", side_effect=RuntimeError("API down")),
            pytest.raises(FingerprintError, match="acoustid lookup failed"),
        ):
            await provider.fingerprint(Path("/tmp/test.mp3"))

    async def test_raises_fingerprint_error_on_import_error(self) -> None:
        """Missing acoustid library is an infrastructure failure, not a no-match."""
        provider = AcoustidFingerprintProvider("test-key")
        original = sys.modules.get("acoustid")
        sys.modules["acoustid"] = None  # type: ignore[assignment]
        try:
            with pytest.raises(FingerprintError, match="acoustid library not installed"):
                await provider.fingerprint(Path("/tmp/test.mp3"))
        finally:
            if original is not None:
                sys.modules["acoustid"] = original
            else:
                sys.modules.pop("acoustid", None)

    async def test_returns_none_when_artist_and_title_empty(self) -> None:
        provider = AcoustidFingerprintProvider("test-key", min_score=0.0)
        fake = _payload([_recording(title="", artist="")])

        with patch("acoustid.match", return_value=fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_preserves_chained_exception_on_lookup_failure(self) -> None:
        """Ensure the original acoustid exception is chained for debugging."""
        provider = AcoustidFingerprintProvider("test-key")
        original_exc = ValueError("bad api key")
        with (
            patch("acoustid.match", side_effect=original_exc),
            pytest.raises(FingerprintError) as exc_info,
        ):
            await provider.fingerprint(Path("/tmp/test.mp3"))
        assert exc_info.value.__cause__ is original_exc

    async def test_picks_duration_match_over_stream_title(self) -> None:
        """Kandidat mit passender Dauer gewinnt gegen StreamTitle-artigen Kandidaten."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        # Reale Datei ist 229s lang — der Kandidat mit duration=229 gewinnt.
        fake = _payload(
            [
                _recording(title="My Blood", artist="twenty one pilots", duration=229.0, sources=696, rid="good"),
                _recording(
                    title="My blood (cover Twenty one pilots)",
                    artist="Radio Tapok",
                    duration=238.97,
                    sources=6,
                    rid="bad",
                ),
            ]
        )
        with (
            patch("acoustid.match", return_value=fake),
            patch("radio_ripper.services.fingerprint.AcoustidFingerprintProvider._read_duration", return_value=229.0),
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.recording_id == "good"
        assert result.title == "My Blood"
        assert result.artist == "twenty one pilots"

    async def test_picks_higher_sources_when_duration_ties(self) -> None:
        """Bei gleicher Duration gewinnt der Kandidat mit mehr Quellen."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        fake = _payload(
            [
                _recording(title="A", duration=200.0, sources=1, rid="few"),
                _recording(title="B", duration=200.0, sources=100, rid="many"),
            ]
        )
        with (
            patch("acoustid.match", return_value=fake),
            patch("radio_ripper.services.fingerprint.AcoustidFingerprintProvider._read_duration", return_value=200.0),
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.recording_id == "many"

    async def test_no_candidate_in_tolerance_returns_none(self) -> None:
        """Kein Kandidat innerhalb der Dauer-Toleranz → kein Match."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.0)
        # Reale Dauer ~200s, Kandidat liegt 30s daneben (> Toleranz 10s)
        fake = _payload([_recording(title="Far Off", duration=170.0, sources=10)])
        with (
            patch("acoustid.match", return_value=fake),
            patch("radio_ripper.services.fingerprint.AcoustidFingerprintProvider._read_duration", return_value=200.0),
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_webservice_error_raised_as_fingerprint_error(self) -> None:
        """AcoustID API errors (invalid key, rate limit) raise WebServiceError;
        our provider must wrap these in FingerprintError so callers can
        distinguish infra failures from genuine no-matches."""
        provider = AcoustidFingerprintProvider("test-key")
        try:
            from acoustid import WebServiceError
        except ImportError:
            pytest.skip("acoustid not installed; WebServiceError unavailable")
        else:
            with (
                patch("acoustid.match", side_effect=WebServiceError("error 5: invalid key")),
                pytest.raises(FingerprintError, match="acoustid lookup failed"),
            ):
                await provider.fingerprint(Path("/tmp/test.mp3"))
