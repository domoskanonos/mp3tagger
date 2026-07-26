# mypy: disable-error-code="no-untyped-call"
"""ID3v2 tagger built on top of :mod:`mutagen`.

:class:`TrackTagger` is the ABC, :class:`ID3Tagger` the default implementation.
Tags written:
    - ``TPE1``  (Artist)
    - ``TPE2``  (Album Artist) — identical to Artist
    - ``TIT2``  (Title)
    - ``TALB``  (Album) — optional
    - ``TYER``  (Year) — optional
    - ``TRSN``  (Internet Radio Station Name) — from provenance
    - ``TPUB``  (Publisher/Label) — radio station name for Jellyfin
    - ``COMM``  (Recorded via radiostream)
    - ``TXXX:RIPPEDBY`` (station@playlist) — provenance
    - ``TLEN``  (Track length in ms) — optional, from iTunes
    - ``TXXX:ITunes*``  (iTunes metadata IDs/URLs) — optional
    - ``APIC``  (Cover art, JPEG or PNG only, scaled 500-1000 px) — optional
"""

from __future__ import annotations

import contextlib
import io
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path

from mutagen.id3 import (
    APIC,
    COMM,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TLEN,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TRSN,
    TSRC,
    TXXX,
    USLT,
    ID3NoHeaderError,
)

from radio_ripper.domain.models import EnrichedInfo, MusicBrainzData, TrackInfo
from radio_ripper.infra.errors import TaggingError

_MIN_COVER_PX = 500
_MAX_COVER_PX = 1000


def _guess_image_mime(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff") or b"JFIF" in data[:20]:
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


def _scale_cover(data: bytes) -> tuple[bytes, str] | None:
    mime = _guess_image_mime(data)
    if mime not in ("image/jpeg", "image/png"):
        return None
    try:
        from PIL import Image

        img: Image.Image = Image.open(io.BytesIO(data))
        w, h = img.size
        long_side = max(w, h)
        if long_side < _MIN_COVER_PX:
            scale = _MIN_COVER_PX / long_side
            img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
            w, h = img.size
            long_side = max(w, h)
        if long_side > _MAX_COVER_PX:
            scale = _MAX_COVER_PX / long_side
            img = img.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        if mime == "image/jpeg":
            if img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            img.save(out, format="JPEG", quality=90)
        else:
            img.save(out, format="PNG")
        return out.getvalue(), mime
    except ImportError:
        return data, mime
    except Exception:
        return data, mime


@contextlib.contextmanager
def _with_audio(file_path: Path, op_name: str = "") -> Iterator[ID3]:
    lsuffix = f" for {op_name}" if op_name else ""
    try:
        audio = _load_or_create(file_path)
    except Exception as exc:
        raise TaggingError(f"failed to load {file_path}{lsuffix}: {exc}") from exc
    yield audio
    try:
        audio.save(file_path, v2_version=3, v1=2)
    except Exception as exc:
        ssuffix = f" {op_name}" if op_name else ""
        raise TaggingError(f"failed to save{ssuffix} to {file_path}: {exc}") from exc


def _embed_apic(audio: ID3, data: bytes, apic_type: int, desc: str) -> None:
    audio.delall(f"APIC:{desc}")
    scaled = _scale_cover(data)
    if scaled is not None:
        scaled_data, mime = scaled
        audio.add(APIC(encoding=3, mime=mime, type=apic_type, desc=desc, data=scaled_data))


def _station_name(provenance: str) -> str:
    return provenance.split("@")[0] if "@" in provenance else provenance


class TrackTagger(ABC):
    """Writes ID3 tags to a recorded MP3 file."""

    @abstractmethod
    def write_all(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
        *,
        enriched: EnrichedInfo | None = None,
        cover_bytes: bytes | None = None,
        fallback_cover: bytes | None = None,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        artist_image: bytes | None = None,
        lyrics: str | None = None,
    ) -> None:
        """Write ALL tags in one save. Combines basic, enriched, fingerprint,
        cover art, artist image, and lyrics into a single mutagen save call."""


def _load_or_create(file_path: Path) -> ID3:
    try:
        return ID3(file_path)
    except ID3NoHeaderError:
        return ID3()


class ID3Tagger(TrackTagger):
    """mutagen-backed ID3 tagger."""

    def write_all(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
        *,
        enriched: EnrichedInfo | None = None,
        cover_bytes: bytes | None = None,
        fallback_cover: bytes | None = None,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        artist_image: bytes | None = None,
        lyrics: str | None = None,
    ) -> None:
        enriched = enriched or EnrichedInfo()
        with _with_audio(file_path, "tags") as audio:
            self._write_all_to(
                audio, track, provenance,
                enriched=enriched, cover_bytes=cover_bytes,
                fallback_cover=fallback_cover, recording_id=recording_id,
                score=score, mb_data=mb_data, artist_image=artist_image,
                lyrics=lyrics,
            )

    @staticmethod
    def _write_all_to(
        audio: ID3,
        track: TrackInfo,
        provenance: str,
        *,
        enriched: EnrichedInfo | None,
        cover_bytes: bytes | None,
        fallback_cover: bytes | None,
        recording_id: str | None,
        score: float,
        mb_data: MusicBrainzData | None,
        artist_image: bytes | None,
        lyrics: str | None,
    ) -> None:
        ALL_FRAMES = (
            "TPE1", "TPE2", "TIT2", "TALB", "TRSN", "TPUB", "COMM", "TXXX:RIPPEDBY",
            "TCON", "TDRC", "TRCK", "TPOS", "APIC", "TLEN",
            "TXXX:ITunesTrackId", "TXXX:ITunesArtistId", "TXXX:ITunesCollectionId",
            "TXXX:ITunesTrackUrl", "TXXX:ITunesPreviewUrl",
            "TXXX:ITunesTrackCount", "TXXX:ITunesDiscCount",
            "TXXX:ITunesCountry", "TXXX:ITunesExplicitness",
            "TXXX:MusicBrainz Recording Id", "TXXX:AcoustID Score",
            "TSRC",
            "TXXX:MusicBrainz Release Id", "TXXX:MusicBrainz Release Group Type",
            "TXXX:MusicBrainz Genres", "TXXX:MusicBrainz Release Title",
            "TXXX:MusicBrainz Release Date", "TXXX:MusicBrainz Album Release Country",
            "TXXX:CatalogNumber", "TXXX:Barcode",
            "USLT", "TXXX:Lyrics",
        )
        for frame in ALL_FRAMES:
            audio.delall(frame)

        # Basic
        if track.artist:
            audio.add(TPE1(encoding=3, text=track.artist))
            audio.add(TPE2(encoding=3, text=track.artist))
        if track.title:
            audio.add(TIT2(encoding=3, text=track.title))

        # Album: enriched → mb_data → track fallback
        album = enriched.album
        if not album and mb_data and mb_data.release_title:
            album = mb_data.release_title
        if not album:
            album = track.title or track.stream_title
        if album:
            audio.add(TALB(encoding=3, text=album))

        # Year: enriched → mb_data.release_date
        year = enriched.year
        if not year and mb_data and mb_data.release_date:
            year = mb_data.release_date[:4]
        if year:
            audio.add(TDRC(encoding=3, text=year))

        # Genre: enriched → mb_data.genres
        genre = enriched.genre
        if not genre and mb_data and mb_data.genres:
            genre = ", ".join(mb_data.genres)
        if genre:
            audio.add(TCON(encoding=3, text=genre))

        audio.add(TRSN(encoding=3, text=_station_name(provenance)))

        # Label: enriched → mb_data.release_label (mb_data wins)
        label = None
        if mb_data and mb_data.release_label:
            label = mb_data.release_label
        elif enriched.label:
            label = enriched.label
        if label:
            audio.add(TPUB(encoding=3, text=label))

        if enriched.track_number is not None:
            trck = str(enriched.track_number)
            if enriched.disc_number is not None:
                trck = f"{enriched.disc_number}/{trck}"
            audio.add(TRCK(encoding=3, text=trck))
        if enriched.disc_number is not None:
            audio.add(TPOS(encoding=3, text=str(enriched.disc_number)))

        # Track length: enriched → mb_data.length_ms (mb_data wins)
        length = None
        if mb_data and mb_data.length_ms is not None:
            length = mb_data.length_ms
        elif enriched.track_length is not None:
            length = enriched.track_length
        if length is not None:
            audio.add(TLEN(encoding=3, text=str(length)))

        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via radiostream"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))

        # iTunes metadata
        it = enriched.itunes_data
        if it:
            if it.track_id is not None:
                audio.add(TXXX(encoding=3, desc="ITunesTrackId", text=str(it.track_id)))
            if it.artist_id is not None:
                audio.add(TXXX(encoding=3, desc="ITunesArtistId", text=str(it.artist_id)))
            if it.collection_id is not None:
                audio.add(TXXX(encoding=3, desc="ITunesCollectionId", text=str(it.collection_id)))
            if it.track_view_url:
                audio.add(TXXX(encoding=3, desc="ITunesTrackUrl", text=it.track_view_url))
            if it.preview_url:
                audio.add(TXXX(encoding=3, desc="ITunesPreviewUrl", text=it.preview_url))
            if it.track_count is not None:
                audio.add(TXXX(encoding=3, desc="ITunesTrackCount", text=str(it.track_count)))
            if it.disc_count is not None:
                audio.add(TXXX(encoding=3, desc="ITunesDiscCount", text=str(it.disc_count)))
            if it.country:
                audio.add(TXXX(encoding=3, desc="ITunesCountry", text=it.country))
            if it.explicitness:
                audio.add(TXXX(encoding=3, desc="ITunesExplicitness", text=it.explicitness))

        # Fingerprint
        if recording_id:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text=recording_id))
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text=str(round(score, 4))))

        if mb_data is not None:
            if mb_data.release_title:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Title", text=mb_data.release_title))
            if mb_data.release_date:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Date", text=mb_data.release_date))
            if mb_data.release_country:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Album Release Country", text=mb_data.release_country))
            if mb_data.isrcs:
                audio.add(TSRC(encoding=3, text=mb_data.isrcs[0]))
            if mb_data.release_id:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Id", text=mb_data.release_id))
            if mb_data.release_group_type:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Group Type", text=mb_data.release_group_type))
            if mb_data.genres:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Genres", text=", ".join(mb_data.genres)))
            if mb_data.release_catalog_no:
                audio.add(TXXX(encoding=3, desc="CatalogNumber", text=mb_data.release_catalog_no))
            if mb_data.barcode:
                audio.add(TXXX(encoding=3, desc="Barcode", text=mb_data.barcode))

        # Cover art
        effective_cover = cover_bytes or fallback_cover
        if effective_cover:
            _embed_apic(audio, effective_cover, 3, "Cover")

        if artist_image is not None:
            _embed_apic(audio, artist_image, 8, "Performer")

        # Lyrics
        if lyrics:
            audio.add(USLT(encoding=1, lang="eng", desc="", text=lyrics))
            audio.add(TXXX(encoding=1, desc="Lyrics", text=lyrics))


__all__ = ["ID3Tagger", "TrackTagger", "_scale_cover"]
