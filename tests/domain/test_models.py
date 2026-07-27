"""Tests for radio_ripper.domain.models."""

from __future__ import annotations

from radio_ripper.domain.models import (
    EnrichedInfo,
    FingerprintResult,
    ITunesTrackData,
    MusicBrainzData,
    TrackInfo,
)


class TestTrackInfoFromStreamTitle:
    def test_standard_dash_separator(self):
        ti = TrackInfo.from_stream_title("Adele - Hello")
        assert ti.stream_title == "Adele - Hello"
        assert ti.artist == "Adele"
        assert ti.title == "Hello"

    def test_em_dash_separator(self):
        ti = TrackInfo.from_stream_title("Adele — Hello")
        assert ti.artist == "Adele"
        assert ti.title == "Hello"

    def test_no_separator_returns_full_as_title(self):
        ti = TrackInfo.from_stream_title("JustASongTitle")
        assert ti.stream_title == "JustASongTitle"
        assert ti.artist == ""
        assert ti.title == "JustASongTitle"

    def test_whitespace_stripped(self):
        ti = TrackInfo.from_stream_title("  Adele - Hello  ")
        assert ti.artist == "Adele"
        assert ti.title == "Hello"

    def test_first_separator_wins(self):
        ti = TrackInfo.from_stream_title("Artist - Title - With - Dashes")
        assert ti.artist == "Artist"
        assert ti.title == "Title - With - Dashes"

    def test_empty_string(self):
        ti = TrackInfo.from_stream_title("")
        assert ti.artist == ""
        assert ti.title == ""

    def test_immutable_frozen_dataclass(self):
        ti = TrackInfo(stream_title="x", artist="a", title="t")
        try:
            ti.artist = "changed"  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass


class TestFingerprintResult:
    def test_construction(self):
        r = FingerprintResult(artist="A", title="T", score=0.9, recording_id="rec-1")
        assert r.artist == "A"
        assert r.title == "T"
        assert r.score == 0.9
        assert r.recording_id == "rec-1"

    def test_immutable(self):
        r = FingerprintResult(artist="A", title="T", score=0.9, recording_id="rec-1")
        try:
            r.score = 0.5  # type: ignore[misc]
            raise AssertionError("Should have raised FrozenInstanceError")
        except AttributeError:
            pass


class TestEnrichedInfo:
    def test_defaults_all_none(self):
        e = EnrichedInfo()
        assert e.artist is None
        assert e.album is None
        assert e.artwork_url is None
        assert e.itunes_data is None

    def test_partial(self):
        e = EnrichedInfo(artist="A", album="Album")
        assert e.artist == "A"
        assert e.album == "Album"
        assert e.title is None


class TestMusicBrainzData:
    def test_required_field(self):
        mb = MusicBrainzData(recording_id="r1")
        assert mb.recording_id == "r1"
        assert mb.recording_artist is None
        assert mb.isrcs == ()

    def test_with_releases(self):
        mb = MusicBrainzData(
            recording_id="r1",
            recording_artist="Artist",
            release_title="Album",
            isrcs=("ISR1", "ISR2"),
        )
        assert mb.recording_artist == "Artist"
        assert mb.release_title == "Album"
        assert mb.isrcs == ("ISR1", "ISR2")


class TestITunesTrackData:
    def test_defaults_all_none(self):
        it = ITunesTrackData()
        assert it.track_id is None
        assert it.artist_id is None
        assert it.country is None

    def test_partial(self):
        it = ITunesTrackData(track_id=123, country="DE")
        assert it.track_id == 123
        assert it.country == "DE"
        assert it.artist_id is None
