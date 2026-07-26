"""Tests for radio_ripper.services.track_processing."""

from __future__ import annotations

from radio_ripper.domain.models import FingerprintResult, MusicBrainzData
from radio_ripper.services.track_processing import correct_fingerprint_result


class TestCorrectFingerprintResult:
    def test_returns_original_when_no_mb_data(self):
        result = FingerprintResult(artist="A", title="B", score=0.95, recording_id="r1")
        assert correct_fingerprint_result(result, None) is result

    def test_returns_original_when_mb_data_no_recording(self):
        result = FingerprintResult(artist="A", title="B", score=0.95, recording_id="r1")
        mb = MusicBrainzData(recording_id="r1", recording_artist=None, recording_title=None)
        assert correct_fingerprint_result(result, mb) is result

    def test_returns_original_when_acoustid_matches_mb(self):
        result = FingerprintResult(artist="Adele", title="Hello", score=0.95, recording_id="r1")
        mb = MusicBrainzData(
            recording_id="r1",
            recording_artist="Adele",
            recording_title="Hello",
        )
        assert correct_fingerprint_result(result, mb) is result

    def test_corrects_swapped_artist_title(self):
        result = FingerprintResult(artist="Hello", title="Adele", score=0.95, recording_id="r1")
        mb = MusicBrainzData(
            recording_id="r1",
            recording_artist="Adele",
            recording_title="Hello",
        )
        corrected = correct_fingerprint_result(result, mb)
        assert corrected.artist == "Adele"
        assert corrected.title == "Hello"
        assert corrected.score == 0.95
        assert corrected.recording_id == "r1"

    def test_corrects_wrong_artist_only(self):
        result = FingerprintResult(artist="Wrong", title="Hello", score=0.90, recording_id="r1")
        mb = MusicBrainzData(
            recording_id="r1",
            recording_artist="Adele",
            recording_title="Hello",
        )
        corrected = correct_fingerprint_result(result, mb)
        assert corrected.artist == "Adele"
        assert corrected.title == "Hello"

    def test_case_insensitive_match_returns_original(self):
        result = FingerprintResult(artist="adele", title="hello", score=0.95, recording_id="r1")
        mb = MusicBrainzData(
            recording_id="r1",
            recording_artist="Adele",
            recording_title="Hello",
        )
        assert correct_fingerprint_result(result, mb) is result
