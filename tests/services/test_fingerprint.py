"""Tests for radio_ripper.services.fingerprint."""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

from radio_ripper.services.fingerprint import (
    AcoustidFingerprintProvider,
    FingerprintError,
)


def _payload(recordings: list[dict], *, score: float = 0.9) -> dict:
    return {"status": "ok", "results": [{"id": "r1", "score": score, "recordings": recordings}]}


def _no_results() -> dict:
    return {"status": "ok", "results": []}


def _patch_lookup(payloads, full_duration: float = 200.0, fingerprint: bytes = b"fp"):
    """Patches fingerprint_file + lookup und liefert einen einzelnen ContextManager.

    ``payloads`` kann ein einzelnes Dict (identische Antwort für jeden
    Dauerwert) oder eine Liste von Dicts sein (ein Eintrag pro Dauerwert, in
    aufsteigender Reihenfolge der Versuche).
    """
    stack = ExitStack()
    stack.enter_context(patch("acoustid.fingerprint_file", return_value=(full_duration, fingerprint)))
    if isinstance(payloads, dict):
        stack.enter_context(patch("acoustid.lookup", return_value=payloads))
    else:
        stack.enter_context(patch("acoustid.lookup", side_effect=payloads))
    return stack


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

        with _patch_lookup(fake):
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

        with _patch_lookup(fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is None

    async def test_returns_none_when_no_results(self) -> None:
        provider = AcoustidFingerprintProvider("test-key")
        with _patch_lookup(_no_results()):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_raises_fingerprint_error_on_acoustid_exception(self) -> None:
        """Infrastructure failures (API down, network) must raise, NOT return None."""
        provider = AcoustidFingerprintProvider("test-key")
        with (
            patch("acoustid.fingerprint_file", return_value=(200.0, b"fp")),
            patch("acoustid.lookup", side_effect=RuntimeError("API down")),
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

        with _patch_lookup(fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None

    async def test_preserves_chained_exception_on_lookup_failure(self) -> None:
        """Ensure the original acoustid exception is chained for debugging."""
        provider = AcoustidFingerprintProvider("test-key")
        original_exc = ValueError("bad api key")
        with (
            patch("acoustid.fingerprint_file", return_value=(200.0, b"fp")),
            patch("acoustid.lookup", side_effect=original_exc),
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
            _patch_lookup(fake),
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
            _patch_lookup(fake),
            patch("radio_ripper.services.fingerprint.AcoustidFingerprintProvider._read_duration", return_value=200.0),
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.recording_id == "many"

    async def test_longer_recording_still_matches_truncated_rip(self) -> None:
        """Gekürzte/truncated Radio-Rips sind KÜRZER als das DB-Recording. Die
        Dauer ist nur Sortier-Präferenz, kein Ausschlusskriterium — sonst würde
        z.B. "Save The Best For Last" (219s Datei, 239s Recording, Score 0.90)
        fälschlich gelöscht."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.0)
        # Reale Dauer ~200s, Kandidat ist 30s LÄNGER als die Datei — trotzdem gültig.
        fake = _payload([_recording(title="Too Long", duration=230.0, sources=10)])
        with (
            _patch_lookup(fake),
            patch("radio_ripper.services.fingerprint.AcoustidFingerprintProvider._read_duration", return_value=200.0),
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.recording_id == "rec123"

    async def test_shorter_recording_still_matches_radio_rip(self) -> None:
        """Radio-Rips sind länger als das Original: Ein kürzeres Recording
        (Jingle/Moderation am Anfang/Ende) darf NICHT verworfen werden."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.0)
        # Datei 267s, Recording nur 241s — typischer Radio-Rip.
        fake = _payload([_recording(title="Summer of 69", artist="Bryan Adams", duration=241.76)])
        with (
            _patch_lookup(fake),
            patch("radio_ripper.services.fingerprint.AcoustidFingerprintProvider._read_duration", return_value=266.8),
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.recording_id == "rec123"

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
                patch("acoustid.fingerprint_file", return_value=(200.0, b"fp")),
                patch("acoustid.lookup", side_effect=WebServiceError("error 5: invalid key")),
                pytest.raises(FingerprintError, match="acoustid lookup failed"),
            ):
                await provider.fingerprint(Path("/tmp/test.mp3"))

    async def test_retries_reduced_duration_when_full_duration_has_no_results(self) -> None:
        """Radio-Rips sind oft länger als das Original — der Lookup mit voller
        Dateilänge liefert dann 0 Ergebnisse, obwohl ein reduzierter Dauerwert
        matcht. Der Provider muss die Dauer-Werte durchprobieren."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        # Datei ist 202s, der Match existiert erst bei duration=200.
        full_duration = 202.0
        fake = _payload([_recording(title="Blinding Lights", artist="The Weeknd", duration=200.0)])
        empty = _no_results()

        lookup_mock = patch("acoustid.lookup", side_effect=[empty, fake])
        fp_mock = patch("acoustid.fingerprint_file", return_value=(full_duration, b"fp"))
        with lookup_mock as mock_lookup, fp_mock:
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is not None
        assert result.title == "Blinding Lights"
        assert result.artist == "The Weeknd"
        # lookup wurde mit voller Länge (202) und dann reduziert (200) aufgerufen
        called_durations = [call.args[2] for call in mock_lookup.call_args_list]
        assert 202 in called_durations
        assert 200 in called_durations

    async def test_low_score_first_result_does_not_stop_duration_search(self) -> None:
        """Ein Treffer mit zu niedrigem Score bei der vollen Dateilänge darf die
        Dauer-Suche nicht abbrechen — ein reduzierter Dauerwert kann deutlich
        besser matchen (beobachtet bei Radio-Rips)."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.85)
        full_duration = 280.0
        # dur=280 liefert nur einen schwachen Treffer, dur=220 den guten.
        weak = _payload(
            [_recording(title="One More Night", artist="Phil Collins", duration=280.0)],
            score=0.66,
        )
        strong = _payload(
            [_recording(title="One More Night", artist="Phil Collins", duration=240.0)],
            score=0.98,
        )

        lookup_mock = patch("acoustid.lookup", side_effect=[weak, strong])
        fp_mock = patch("acoustid.fingerprint_file", return_value=(full_duration, b"fp"))
        with lookup_mock as mock_lookup, fp_mock:
            result = await provider.fingerprint(Path("/tmp/test.mp3"))

        assert result is not None
        assert result.score >= 0.85
        # Es wurden mehrere Dauerwerte probiert (nicht nach dem ersten abgebrochen)
        assert mock_lookup.call_count >= 2
        called_durations = [call.args[2] for call in mock_lookup.call_args_list]
        assert 280 in called_durations
        assert len(called_durations) == 2

    async def test_no_result_after_all_duration_retries_returns_none(self) -> None:
        """Wenn kein Dauerwert Treffer liefert, ist es ein echter No-Match."""
        provider = AcoustidFingerprintProvider("test-key")
        empty = _no_results()
        # Dauerwerte 202, 200, 198, ... — alle ohne Treffer.
        durations = AcoustidFingerprintProvider._candidate_durations(202.0)
        with (
            patch("acoustid.fingerprint_file", return_value=(202.0, b"fp")),
            patch("acoustid.lookup", return_value=empty) as lookup_mock,
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is None
        assert lookup_mock.call_count == len(durations)

    def test_candidate_durations_descend_from_full(self) -> None:
        """Die Kandidaten-Dauern starten bei der vollen Dateilänge und steigen ab."""
        durations = AcoustidFingerprintProvider._candidate_durations(202.0)
        assert durations[0] == 202
        assert durations == sorted(set(durations), reverse=True)
        assert all(d >= 30 for d in durations)

    def test_candidate_durations_respect_minimum(self) -> None:
        """Kurze Dateien dürfen nicht unter die Untergrenze fallen."""
        durations = AcoustidFingerprintProvider._candidate_durations(35.0)
        assert durations[0] == 35
        assert all(d >= 30 for d in durations)

    async def test_handles_usermeta_artists_as_strings(self) -> None:
        """Mit ``usermeta`` liefert AcoustID ``artists`` teils als reine Strings —
        das darf nicht crashen, sondern muss als Artist-Name übernommen werden."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        fake = _payload([{"id": "rec-str", "title": "Blinding Lights", "artists": ["The Weeknd"]}])
        with _patch_lookup(fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.artist == "The Weeknd"
        assert result.recording_id == "rec-str"

    async def test_accepts_candidate_without_recording_id(self) -> None:
        """usermeta-Treffer ohne MBID (leere recording_id) dürfen NICHT verworfen
        werden — artist/title + Score reichen aus."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.85)
        fake = _payload(
            [{"id": None, "title": "Blinding Lights (Lyrics)", "artists": ["The Weeknd"]}],
            score=0.86,
        )
        with _patch_lookup(fake):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.recording_id == ""
        assert result.title == "Blinding Lights (Lyrics)"
        assert result.artist == "The Weeknd"

    async def test_api_error_raises_fingerprint_error_not_no_match(self) -> None:
        """Ein API-Fehler (status='error', z.B. 429/500) muss als FingerprintError
        hochgereicht werden — NICHT als 'kein Treffer' (None), sonst löscht der
        Caller die Datei. Nach den Retries wird der Fehler propagiert."""
        from radio_ripper.services.fingerprint import FingerprintError

        provider = AcoustidFingerprintProvider("test-key")
        error_payload = {"status": "error", "error": {"code": 9, "message": "Too many requests"}}
        with (
            patch("acoustid.fingerprint_file", return_value=(200.0, b"fp")),
            patch("acoustid.lookup", return_value=error_payload) as lookup_mock,
            patch("radio_ripper.services.fingerprint.asyncio.sleep") as sleep_mock,
            pytest.raises(FingerprintError, match="AcoustID API error"),
        ):
            await provider.fingerprint(Path("/tmp/test.mp3"))
        assert lookup_mock.call_count == 3
        assert sleep_mock.call_count == 2  # nach den ersten beiden Fehlversuchen

    async def test_throttle_waits_10s_then_recovers(self) -> None:
        """Erst Throttle (status='error'), dann Erfolg: Es muss 10s gewartet und
        erneut versucht werden, und der Treffer wird zurückgegeben."""
        provider = AcoustidFingerprintProvider("test-key", min_score=0.5)
        error_payload = {"status": "error", "error": {"code": 9, "message": "Too many requests"}}
        fake = _payload([_recording(title="Blinding Lights", artist="The Weeknd")])
        with (
            patch("acoustid.fingerprint_file", return_value=(200.0, b"fp")),
            patch("acoustid.lookup", side_effect=[error_payload, fake]) as lookup_mock,
            patch("radio_ripper.services.fingerprint.asyncio.sleep") as sleep_mock,
        ):
            result = await provider.fingerprint(Path("/tmp/test.mp3"))
        assert result is not None
        assert result.title == "Blinding Lights"
        assert lookup_mock.call_count == 2
        sleep_mock.assert_awaited_once_with(10.0)

    async def test_permanent_api_error_fails_fast(self) -> None:
        """Ein permanenter API-Fehler (z.B. invalid API key, code 4) muss SOFORT
        als FingerprintError propagiert werden — kein 10s-Warten/Retry."""
        from radio_ripper.services.fingerprint import FingerprintError

        provider = AcoustidFingerprintProvider("test-key")
        error_payload = {"status": "error", "error": {"code": 4, "message": "invalid API key"}}
        with (
            patch("acoustid.fingerprint_file", return_value=(200.0, b"fp")),
            patch("acoustid.lookup", return_value=error_payload) as lookup_mock,
            patch("radio_ripper.services.fingerprint.asyncio.sleep") as sleep_mock,
            pytest.raises(FingerprintError, match="code=4"),
        ):
            await provider.fingerprint(Path("/tmp/test.mp3"))
        assert lookup_mock.call_count == 1
        sleep_mock.assert_not_awaited()
