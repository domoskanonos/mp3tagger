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
import logging
from abc import ABC, abstractmethod
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
from radio_ripper.services.metadata import MetadataProvider

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
    """Scale *data* to the 500-1000 px target range and return ``(bytes, mime)``.

    Only ``image/jpeg`` and ``image/png`` are accepted; any other format
    (e.g. GIF) returns ``None`` so the cover is silently skipped.
    On Pillow import error or decode failure the original bytes are returned
    unchanged so the cover is still embedded without scaling.
    """
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
        with contextlib.suppress(Exception):
            pass
        return data, mime


class TrackTagger(ABC):
    """Writes ID3 tags to a recorded MP3 file."""

    @abstractmethod
    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        """Write minimal tags (artist/title/comment) synchronously."""

    @abstractmethod
    def write_full(
        self,
        file_path: Path,
        track: TrackInfo,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        provenance: str,
        *,
        fallback_cover: bytes | None = None,
    ) -> None:
        """Write enriched tags including album/year/genre and cover art."""

    @abstractmethod
    def update_acoustid(self, file_path: Path, recording_id: str, score: float) -> None:
        """Add AcoustID/MusicBrainz tags to an already-tagged file."""

    @abstractmethod
    def embed_cover(self, file_path: Path, cover_bytes: bytes) -> None:
        """Embed cover-art bytes into an existing file (replaces any APIC)."""

    @abstractmethod
    def update_musicbrainz_metadata(
        self,
        file_path: Path,
        mb_data: MusicBrainzData,
    ) -> None:
        """Write MusicBrainz metadata (TPUB, TSRC, TLEN, TXXX) after an AcoustID match."""

    @abstractmethod
    def write_fingerprint_tags(
        self,
        file_path: Path,
        *,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        cover_bytes: bytes | None = None,
        artist_image: bytes | None = None,
    ) -> None:
        """Write all post-fingerprint tags in a single save.

        Combines AcoustID, MusicBrainz, cover art, and artist image
        into one mutagen save call to reduce disk I/O.
        """

    @abstractmethod
    def write_lyrics(self, file_path: Path, lyrics: str) -> None:
        """Write lyrics text into the USLT frame."""

    @abstractmethod
    def write_artist_image(self, file_path: Path, image_bytes: bytes) -> None:
        """Embed artist portrait into APIC type=8 (Cover (front) stays type=3)."""


def _load_or_create(file_path: Path) -> ID3:
    """Load an existing ID3 tag or create a fresh one.

    Raises the underlying mutagen error (e.g. ``MutagenError``) if the file
    exists but cannot be read, or the file does not exist and the parent
    directory is missing.
    """
    try:
        return ID3(file_path)
    except ID3NoHeaderError:
        return ID3()


class ID3Tagger(TrackTagger):
    """mutagen-backed ID3 tagger."""

    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path}: {exc}") from exc
        audio.delall("TPE1")
        audio.delall("TPE2")
        audio.delall("TIT2")
        audio.delall("TALB")
        audio.delall("TRSN")
        audio.delall("TPUB")
        audio.delall("COMM")
        audio.delall("TXXX:RIPPEDBY")
        if track.artist:
            audio.add(TPE1(encoding=3, text=track.artist))
            audio.add(TPE2(encoding=3, text=track.artist))
        if track.title:
            audio.add(TIT2(encoding=3, text=track.title))
        # Album fallback: prefer the song title (without artist) over empty,
        # so the player doesn't fall back to the station name.
        audio.add(TALB(encoding=3, text=track.title or track.stream_title))
        # Extract station name from provenance (format: "station@url")
        station_name = provenance.split("@")[0] if "@" in provenance else provenance
        audio.add(TRSN(encoding=3, text=station_name))
        # TPUB intentionally omitted — written only when we have a real label
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via radiostream"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save basic tags to {file_path}: {exc}") from exc

    def write_full(
        self,
        file_path: Path,
        track: TrackInfo,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        provenance: str,
        *,
        fallback_cover: bytes | None = None,
    ) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path}: {exc}") from exc
        audio.delall("TPE1")
        audio.delall("TPE2")
        audio.delall("TIT2")
        audio.delall("TALB")
        audio.delall("TCON")
        audio.delall("TDRC")
        audio.delall("TRCK")
        audio.delall("TPOS")
        audio.delall("TRSN")
        audio.delall("TPUB")
        audio.delall("COMM")
        audio.delall("APIC")
        audio.delall("TXXX:RIPPEDBY")
        audio.delall("TLEN")
        audio.delall("TXXX:ITunesTrackId")
        audio.delall("TXXX:ITunesArtistId")
        audio.delall("TXXX:ITunesCollectionId")
        audio.delall("TXXX:ITunesTrackUrl")
        audio.delall("TXXX:ITunesPreviewUrl")
        audio.delall("TXXX:ITunesTrackCount")
        audio.delall("TXXX:ITunesDiscCount")
        audio.delall("TXXX:ITunesCountry")
        audio.delall("TXXX:ITunesExplicitness")

        artist = enriched.artist or track.artist
        title = enriched.title or track.title
        if artist:
            audio.add(TPE1(encoding=3, text=artist))
            audio.add(TPE2(encoding=3, text=artist))
        if title:
            audio.add(TIT2(encoding=3, text=title))
        if enriched.album:
            audio.add(TALB(encoding=3, text=enriched.album))
        else:
            # Fallback: prefer song title (no artist) over empty so the player
            # doesn't fall back to the station name as album placeholder.
            audio.add(TALB(encoding=3, text=track.title or track.stream_title))
        if enriched.year:
            audio.add(TDRC(encoding=3, text=enriched.year))
        if enriched.genre:
            audio.add(TCON(encoding=3, text=enriched.genre))
        # Extract station name from provenance (format: "station@url")
        station_name = provenance.split("@")[0] if "@" in provenance else provenance
        audio.add(TRSN(encoding=3, text=station_name))
        # Label: only write when we have actual label data — never fall back to station name
        if enriched.label:
            audio.add(TPUB(encoding=3, text=enriched.label))

        if enriched.track_number is not None:
            trck = str(enriched.track_number)
            if enriched.disc_number is not None:
                trck = f"{enriched.disc_number}/{trck}"
            audio.add(TRCK(encoding=3, text=trck))
        if enriched.disc_number is not None:
            audio.add(TPOS(encoding=3, text=str(enriched.disc_number)))
        if enriched.track_length is not None:
            audio.add(TLEN(encoding=3, text=str(enriched.track_length)))
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via radiostream"))
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))

        # iTunes ancillary TXXX frames
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
        effective_cover = cover_bytes or fallback_cover
        if effective_cover:
            scaled = _scale_cover(effective_cover)
            if scaled is not None:
                scaled_data, mime = scaled
                audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=scaled_data))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save enriched tags to {file_path}: {exc}") from exc

    def update_acoustid(self, file_path: Path, recording_id: str, score: float) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path} for acoustid tag: {exc}") from exc
        audio.delall("TXXX:MusicBrainz Recording Id")
        audio.delall("TXXX:AcoustID Score")
        if recording_id:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text=recording_id))
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text=str(round(score, 4))))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save acoustid tags to {file_path}: {exc}") from exc

    def embed_cover(self, file_path: Path, cover_bytes: bytes) -> None:
        """Replace any existing APIC frame with the supplied cover bytes."""
        scaled = _scale_cover(cover_bytes)
        if scaled is not None:
            scaled_data, mime = scaled
            try:
                audio = _load_or_create(file_path)
            except Exception as exc:
                raise TaggingError(f"failed to load {file_path} for cover embed: {exc}") from exc
            audio.delall("APIC:Cover")
            audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=scaled_data))
            try:
                audio.save(file_path, v2_version=3, v1=2)
            except Exception as exc:
                raise TaggingError(f"failed to save cover to {file_path}: {exc}") from exc

    def write_artist_image(self, file_path: Path, image_bytes: bytes) -> None:
        """Embed artist portrait into APIC type=8 (Cover stays type=3)."""
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path} for artist image: {exc}") from exc
        audio.delall("APIC:Performer")
        scaled = _scale_cover(image_bytes)
        if scaled is not None:
            scaled_data, mime = scaled
            audio.add(APIC(encoding=3, mime=mime, type=8, desc="Performer", data=scaled_data))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save artist image to {file_path}: {exc}") from exc

    def update_musicbrainz_metadata(
        self,
        file_path: Path,
        mb_data: MusicBrainzData,
    ) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path} for MB metadata: {exc}") from exc
        audio.delall("TSRC")
        audio.delall("TLEN")
        audio.delall("TXXX:MusicBrainz Release Id")
        audio.delall("TXXX:MusicBrainz Release Group Type")
        audio.delall("TXXX:MusicBrainz Genres")
        audio.delall("TXXX:MusicBrainz Release Title")
        audio.delall("TXXX:MusicBrainz Release Date")
        audio.delall("TXXX:MusicBrainz Album Release Country")
        audio.delall("TXXX:CatalogNumber")
        audio.delall("TXXX:Barcode")

        if mb_data.release_label:
            audio.delall("TPUB")
            audio.add(TPUB(encoding=3, text=mb_data.release_label))
        if mb_data.release_title:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Release Title", text=mb_data.release_title))
        if mb_data.release_date:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Release Date", text=mb_data.release_date))
        if mb_data.release_country:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Album Release Country", text=mb_data.release_country))
        if mb_data.isrcs:
            audio.add(TSRC(encoding=3, text=mb_data.isrcs[0]))
        if mb_data.length_ms is not None:
            audio.add(TLEN(encoding=3, text=str(mb_data.length_ms)))
        if mb_data.release_id:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Release Id", text=mb_data.release_id))
        if mb_data.release_group_type:
            audio.add(
                TXXX(
                    encoding=3,
                    desc="MusicBrainz Release Group Type",
                    text=mb_data.release_group_type,
                )
            )
        if mb_data.genres:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Genres", text=", ".join(mb_data.genres)))
        if mb_data.release_catalog_no:
            audio.add(TXXX(encoding=3, desc="CatalogNumber", text=mb_data.release_catalog_no))
        if mb_data.barcode:
            audio.add(TXXX(encoding=3, desc="Barcode", text=mb_data.barcode))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save MB metadata to {file_path}: {exc}") from exc

    def write_fingerprint_tags(
        self,
        file_path: Path,
        *,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        cover_bytes: bytes | None = None,
        artist_image: bytes | None = None,
    ) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path} for fingerprint tags: {exc}") from exc

        # AcoustID
        audio.delall("TXXX:MusicBrainz Recording Id")
        audio.delall("TXXX:AcoustID Score")
        if recording_id:
            audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text=recording_id))
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text=str(round(score, 4))))

        if mb_data is not None:
            audio.delall("TSRC")
            audio.delall("TLEN")
            audio.delall("TXXX:MusicBrainz Release Id")
            audio.delall("TXXX:MusicBrainz Release Group Type")
            audio.delall("TXXX:MusicBrainz Genres")
            audio.delall("TXXX:MusicBrainz Release Title")
            audio.delall("TXXX:MusicBrainz Release Date")
            audio.delall("TXXX:MusicBrainz Album Release Country")
            audio.delall("TXXX:CatalogNumber")
            audio.delall("TXXX:Barcode")
            if mb_data.release_label:
                audio.delall("TPUB")
                audio.add(TPUB(encoding=3, text=mb_data.release_label))
            if mb_data.release_title:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Title", text=mb_data.release_title))
            if mb_data.release_date:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Date", text=mb_data.release_date))
            if mb_data.release_country:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Album Release Country", text=mb_data.release_country))
            if mb_data.isrcs:
                audio.add(TSRC(encoding=3, text=mb_data.isrcs[0]))
            if mb_data.length_ms is not None:
                audio.add(TLEN(encoding=3, text=str(mb_data.length_ms)))
            if mb_data.release_id:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Id", text=mb_data.release_id))
            if mb_data.release_group_type:
                audio.add(
                    TXXX(encoding=3, desc="MusicBrainz Release Group Type", text=mb_data.release_group_type)
                )
            if mb_data.genres:
                audio.add(TXXX(encoding=3, desc="MusicBrainz Genres", text=", ".join(mb_data.genres)))
            if mb_data.release_catalog_no:
                audio.add(TXXX(encoding=3, desc="CatalogNumber", text=mb_data.release_catalog_no))
            if mb_data.barcode:
                audio.add(TXXX(encoding=3, desc="Barcode", text=mb_data.barcode))

        if cover_bytes is not None:
            audio.delall("APIC:Cover")
            scaled = _scale_cover(cover_bytes)
            if scaled is not None:
                scaled_data, mime = scaled
                audio.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=scaled_data))

        if artist_image is not None:
            audio.delall("APIC:Performer")
            scaled = _scale_cover(artist_image)
            if scaled is not None:
                scaled_data, mime = scaled
                audio.add(APIC(encoding=3, mime=mime, type=8, desc="Performer", data=scaled_data))

        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save fingerprint tags to {file_path}: {exc}") from exc

    def write_lyrics(self, file_path: Path, lyrics: str) -> None:
        try:
            audio = _load_or_create(file_path)
        except Exception as exc:
            raise TaggingError(f"failed to load {file_path} for lyrics: {exc}") from exc
        audio.delall("USLT")
        audio.delall("TXXX:Lyrics")
        audio.add(USLT(encoding=1, lang="eng", desc="", text=lyrics))
        # Some Android players look for TXXX:Lyrics instead of USLT
        audio.add(TXXX(encoding=1, desc="Lyrics", text=lyrics))
        try:
            audio.save(file_path, v2_version=3, v1=2)
        except Exception as exc:
            raise TaggingError(f"failed to save lyrics to {file_path}: {exc}") from exc


class NullTagger(TrackTagger):
    """No-op tagger (used when tagging is disabled)."""

    def write_basic(self, file_path: Path, track: TrackInfo, provenance: str) -> None:
        return None

    def write_full(
        self,
        file_path: Path,
        track: TrackInfo,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        provenance: str,
        *,
        fallback_cover: bytes | None = None,
    ) -> None:
        return None

    def update_acoustid(self, file_path: Path, recording_id: str, score: float) -> None:
        return None

    def embed_cover(self, file_path: Path, cover_bytes: bytes) -> None:
        return None

    def update_musicbrainz_metadata(self, file_path: Path, mb_data: MusicBrainzData) -> None:
        return None

    def write_fingerprint_tags(
        self,
        file_path: Path,
        *,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        cover_bytes: bytes | None = None,
        artist_image: bytes | None = None,
    ) -> None:
        return None

    def write_lyrics(self, file_path: Path, lyrics: str) -> None:
        return None

    def write_artist_image(self, file_path: Path, image_bytes: bytes) -> None:
        return None


async def enrich_and_tag(
    metadata_provider: MetadataProvider,
    tagger: TrackTagger,
    file_path: Path,
    track: TrackInfo,
    provenance: str,
    *,
    fallback_cover_path: Path | None = None,
    embed_cover_art: bool = True,
    logger: logging.Logger | None = None,
) -> EnrichedInfo | None:
    """Fetch iTunes metadata, download cover art, write enriched ID3 tags.

    Returns the enriched info when a match was found, or ``None`` when no
    metadata was returned (fallback cover is still embedded if configured).
    """
    info = await metadata_provider.fetch(track.artist, track.title)

    fallback_cover: bytes | None = None
    if fallback_cover_path is not None:
        with contextlib.suppress(OSError):
            fallback_cover = fallback_cover_path.read_bytes()

    if info is None:
        if fallback_cover and embed_cover_art:
            try:
                tagger.write_full(
                    file_path,
                    track,
                    EnrichedInfo(),
                    None,
                    provenance,
                    fallback_cover=fallback_cover,
                )
            except Exception as exc:
                if logger:
                    logger.warning(
                        "[%s] fallback-cover embed failed %s: %s",
                        track.stream_title,
                        file_path.name,
                        exc,
                    )
        return None

    cover: bytes | None = None
    if embed_cover_art and info.artwork_url:
        cover = await metadata_provider.download_image(info.artwork_url)

    try:
        tagger.write_full(file_path, track, info, cover, provenance, fallback_cover=fallback_cover)
    except Exception as exc:
        if logger:
            logger.warning("[%s] tag-enrichment failed %s: %s", track.stream_title, file_path.name, exc)

    return info


__all__ = ["ID3Tagger", "NullTagger", "TrackTagger", "_scale_cover", "enrich_and_tag"]
