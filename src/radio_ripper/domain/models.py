"""Domain models — plain data carriers free of infrastructure concerns."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TrackInfo:
    """Immutable track description parsed from the ICY StreamTitle.

    Attributes:
        stream_title: Raw original ``StreamTitle`` string.
        artist: Best-effort artist segmentation, or empty string if not separable.
        title: Song title portion (or entire stream_title if no separator found).
    """

    stream_title: str
    artist: str
    title: str

    @classmethod
    def from_stream_title(cls, stream_title: str) -> TrackInfo:
        """Create a :class:`TrackInfo` by splitting ``Artist - Title``."""
        stream_title = stream_title.strip()
        for sep in (" - ", " — "):
            if sep in stream_title:
                artist, _, song = stream_title.partition(sep)
                return cls(stream_title, artist.strip(), song.strip())
        return cls(stream_title, "", stream_title)


@dataclass(frozen=True, slots=True)
class ITunesTrackData:
    """Raw fields from the iTunes Search API result that don't map to standard ID3 frames."""

    track_id: int | None = None
    artist_id: int | None = None
    collection_id: int | None = None
    track_view_url: str | None = None
    preview_url: str | None = None
    track_count: int | None = None
    disc_count: int | None = None
    country: str | None = None
    explicitness: str | None = None


@dataclass(frozen=True, slots=True)
class MusicBrainzData:
    """Metadata fetched from MusicBrainz after an AcoustID match.

    Requires two API calls (recording → releases → labels) so it's populated
    lazily in :meth:`~radio_ripper.services.metadata.CoverArtArchiveProvider.fetch_recording_data`.
    """

    recording_id: str
    recording_title: str | None = None
    recording_artist: str | None = None
    length_ms: int | None = None
    isrcs: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    release_id: str | None = None
    release_title: str | None = None
    release_label: str | None = None
    release_catalog_no: str | None = None
    release_date: str | None = None
    release_country: str | None = None
    release_group_type: str | None = None
    barcode: str | None = None


@dataclass(frozen=True, slots=True)
class EnrichedInfo:
    """Metadata enrichment results fetched from external providers (e.g. iTunes).

    All fields are optional — providers may only fill part of them.
    """

    artist: str | None = None
    title: str | None = None
    album: str | None = None
    year: str | None = None
    genre: str | None = None
    label: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    track_length: int | None = None
    artwork_url: str | None = None
    itunes_data: ITunesTrackData | None = None


@dataclass(frozen=True, slots=True)
class FingerprintResult:
    """Result from AcoustID/MusicBrainz fingerprinting.

    Attributes:
        artist: Matched artist name.
        title: Matched track title.
        score: Confidence score (0.0-1.0).
        recording_id: MusicBrainz recording MBID.
    """

    artist: str
    title: str
    score: float
    recording_id: str


__all__ = [
    "EnrichedInfo",
    "FingerprintResult",
    "ITunesTrackData",
    "MusicBrainzData",
    "TrackInfo",
]
