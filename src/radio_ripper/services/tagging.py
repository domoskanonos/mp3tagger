# mypy: disable-error-code="no-untyped-call"
# pyright: reportPrivateImportUsage=false
"""ID3v2-Tagger basierend auf :mod:`mutagen`.

:class:`TrackTagger` ist das ABC, :class:`ID3Tagger` die Standard-Implementierung.
Geschriebene Tags:
    - ``TPE1``  (Interpret)
    - ``TPE2``  (Album-Interpret) — identisch zu TPE1
    - ``TIT2``  (Titel)
    - ``TALB``  (Album) — optional
    - ``TYER``  (Jahr) — optional
    - ``TRSN``  (Internetsender-Name) — aus der Provenance
    - ``TPUB``  (Label/Verlag)
    - ``COMM``  (Recorded via radiostream)
    - ``TXXX:RIPPEDBY`` (Station@Playlist) — Provenance
    - ``TLEN``  (Titel-Länge in ms) — optional, von iTunes/MusicBrainz
    - ``TXXX:ITunes*``  (iTunes-Metadaten) — optional
    - ``APIC``  (Cover, JPEG/PNG, skaliert auf 500-1000 px) — optional
"""

from __future__ import annotations

import contextlib
import io
from abc import ABC, abstractmethod
from collections.abc import Iterator
from pathlib import Path
from typing import Any

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
from radio_ripper.services.metadata_deezer import DeezerData

_MIN_COVER_PX = 500
_MAX_COVER_PX = 1000


# ── Hilfsfunktionen ──


def _guess_image_mime(data: bytes) -> str:
    """Ermittelt den MIME-Typ eines Bildes anhand der Magic Bytes."""
    if data.startswith(b"\xff\xd8\xff") or b"JFIF" in data[:20]:
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    return "image/jpeg"


def _scale_cover(data: bytes) -> tuple[bytes, str] | None:
    """Skaliert ein Cover-Bild auf 500-1000 px (längste Seite).

    Nur JPEG und PNG werden akzeptiert — GIF wird still ignoriert.
    Wenn Pillow fehlt oder das Bild nicht dekodiert werden kann,
    werden die Original-Daten unverändert zurückgegeben.
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
        return data, mime


@contextlib.contextmanager
def _tag_edit_context(file_path: Path, op_name: str = "") -> Iterator[ID3]:
    """Lädt eine MP3-Datei als ID3-Objekt, gibt sie zum Editieren frei
    und speichert sie beim Verlassen des Context-Managers wieder.

    Fehler beim Laden oder Speichern werden als TaggingError geworfen.
    """
    lsuffix = f" für {op_name}" if op_name else ""
    try:
        audio = _load_or_create(file_path)
    except Exception as exc:
        raise TaggingError(f"Datei konnte nicht geladen werden {file_path}{lsuffix}: {exc}") from exc
    yield audio
    try:
        audio.save(file_path, v2_version=3, v1=2)
    except Exception as exc:
        ssuffix = f" {op_name}" if op_name else ""
        raise TaggingError(f"Speichern fehlgeschlagen{ssuffix} für {file_path}: {exc}") from exc


def _embed_apic(audio: ID3, data: bytes, apic_type: int, desc: str) -> None:
    """Bettet ein Bild als APIC-Frame in das ID3-Objekt ein.

    Vorhandene Frames mit demselben Namen (z.B. APIC:Cover) werden vorher
    gelöscht. Das Bild wird vor dem Einbetten via _scale_cover skaliert.
    """
    audio.delall(f"APIC:{desc}")
    scaled = _scale_cover(data)
    if scaled is not None:
        scaled_data, mime = scaled
        audio.add(APIC(encoding=3, mime=mime, type=apic_type, desc=desc, data=scaled_data))


def _existing_text(audio: ID3, frame_id: str) -> str | None:
    """Liest den Text eines vorhandenen ID3-Frames; ``None`` wenn nicht existiert."""
    frame = audio.get(frame_id)
    if frame is not None:
        with contextlib.suppress(Exception):
            text = frame.text[0] if hasattr(frame, "text") else str(frame)
            return str(text) if text else None
    return None


def _is_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    stripped = value.strip()
    return stripped.startswith("[") and stripped.endswith("]")


def _station_name(provenance: str) -> str:
    """Extrahiert den Sendernamen aus der Provenance-Zeichenkette.

    Provenance-Format: ``sendername@playlistname`` → ``sendername``.
    """
    return provenance.split("@")[0] if "@" in provenance else provenance


def _load_or_create(file_path: Path) -> ID3:
    """Lädt vorhandene ID3-Tags oder erzeugt ein leeres ID3-Objekt."""
    try:
        return ID3(file_path)
    except ID3NoHeaderError:
        return ID3()


def read_acoustid_score(path: Path) -> float | None:
    """Liest den AcoustID-Score aus den ID3-Tags einer MP3-Datei.

    Gibt ``None`` zurück, wenn die Datei keine Tags hat oder kein
    ``TXXX:AcoustID Score``-Frame vorhanden ist.
    """
    try:
        audio = ID3(path)
    except Exception:
        return None
    for frame in audio.getall("TXXX"):
        if frame.desc == "AcoustID Score":
            try:
                return float(frame.text[0])
            except (ValueError, IndexError):
                return None
    return None


# ── ABC ──


class TrackTagger(ABC):
    """Abstrakter ID3-Tagger. Die einzige öffentliche Methode ist ``write_all``."""

    @abstractmethod
    def write_all(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
        *,
        enriched: EnrichedInfo | None = None,
        cover_bytes: bytes | None = None,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        artist_image: bytes | None = None,
        lyrics: str | None = None,
        deezer: DeezerData | None = None,
    ) -> None:
        """Schreibt ALLE Tags in einem einzigen Durchgang."""


# ── Implementierung ──


class ID3Tagger(TrackTagger):
    """mutagen-basierte ID3-Tagger-Implementierung."""

    def write_all(
        self,
        file_path: Path,
        track: TrackInfo,
        provenance: str,
        *,
        enriched: EnrichedInfo | None = None,
        cover_bytes: bytes | None = None,
        recording_id: str | None = None,
        score: float = 0.0,
        mb_data: MusicBrainzData | None = None,
        artist_image: bytes | None = None,
        lyrics: str | None = None,
        deezer: DeezerData | None = None,
    ) -> None:
        enriched = enriched or EnrichedInfo()
        with _tag_edit_context(file_path, "tags") as audio:
            self._write_all_to(
                audio,
                track,
                provenance,
                enriched=enriched,
                cover_bytes=cover_bytes,
                recording_id=recording_id,
                score=score,
                mb_data=mb_data,
                artist_image=artist_image,
                lyrics=lyrics,
                deezer=deezer,
            )

    @staticmethod
    def _write_all_to(
        audio: ID3,
        track: TrackInfo,
        provenance: str,
        *,
        enriched: EnrichedInfo,
        cover_bytes: bytes | None,
        recording_id: str | None,
        score: float,
        mb_data: MusicBrainzData | None,
        artist_image: bytes | None,
        lyrics: str | None,
        deezer: DeezerData | None = None,
    ) -> None:
        """Schreibt ID3-Frames mergend in das *audio*-Objekt.

        Pro Feld: neuen Wert aus API-Daten nehmen, wenn vorhanden,
        sonst existierenden Frame im audio-Objekt belassen.
        """

        def _set_frame(frame_id: str, factory: Any, new_value: object) -> None:
            # Leerer Wert → bestehenden (ggf. korrekten) Frame NICHT antasten.
            if not new_value:
                return
            current = audio.get(frame_id)
            old = str(current) if current is not None else ""
            new = str(new_value)
            if new != old:
                audio.delall(frame_id)
                audio.add(factory(encoding=3, text=new_value))

        # ── Basis-Frames ──
        if track.artist:
            _set_frame("TPE1", TPE1, track.artist)
            _set_frame("TPE2", TPE2, track.artist)
        if track.title:
            _set_frame("TIT2", TIT2, track.title)

        # Album: deezer → enriched → mb_data (kein Dummy-Fallback —
        # ohne echte Quelle bleibt TALB ehrlich leer statt des Track-Titels).
        album = deezer.album if deezer else None
        if not album:
            album = enriched.album
        if not album and mb_data and mb_data.release_title:
            album = mb_data.release_title
        if album:
            _set_frame("TALB", TALB, album)

        # Jahr: deezer → enriched → mb_data → vorhanden
        year = deezer.year if deezer else None
        if not year:
            year = enriched.year
        if not year and mb_data and mb_data.release_date:
            year = mb_data.release_date[:4]
        if year:
            _set_frame("TDRC", TDRC, year)

        # Genre: deezer → enriched → mb_data → vorhanden (nie [...])
        genre = deezer.genre if deezer else None
        if _is_placeholder(genre):
            genre = None
        if not genre:
            genre = enriched.genre
        if _is_placeholder(genre):
            genre = None
        if not genre and mb_data and mb_data.genres:
            genre = ", ".join(mb_data.genres)
        if _is_placeholder(genre):
            genre = None
        if not genre:
            genre = _existing_text(audio, "TCON")
        if genre and not _is_placeholder(genre):
            _set_frame("TCON", TCON, genre)

        _set_frame("TRSN", TRSN, _station_name(provenance))

        # Label: deezer → mb_data → enriched → vorhanden (nie [...])
        label = deezer.label if deezer else None
        if _is_placeholder(label):
            label = None
        if not label:
            label = mb_data.release_label if mb_data else None
        if _is_placeholder(label):
            label = None
        if not label:
            label = enriched.label
        if _is_placeholder(label):
            label = None
        if not label:
            label = _existing_text(audio, "TPUB")
        if label and not _is_placeholder(label):
            _set_frame("TPUB", TPUB, label)

        # Track-/Disc-Nummer
        if enriched.track_number is not None:
            trck = str(enriched.track_number)
            if enriched.disc_number is not None:
                trck = f"{enriched.disc_number}/{trck}"
            _set_frame("TRCK", TRCK, trck)
        if enriched.disc_number is not None:
            _set_frame("TPOS", TPOS, str(enriched.disc_number))

        # Titel-Länge: mb_data → deezer → enriched
        length = None
        if mb_data and mb_data.length_ms is not None:
            length = mb_data.length_ms
        elif deezer and deezer.duration_s is not None:
            length = deezer.duration_s * 1000
        elif enriched.track_length is not None:
            length = enriched.track_length
        if length is not None:
            _set_frame("TLEN", TLEN, str(length))

        audio.delall("COMM")
        audio.add(COMM(encoding=3, lang="eng", desc="", text="Recorded via radiostream"))
        audio.delall("TXXX:RIPPEDBY")
        audio.add(TXXX(encoding=3, desc="RIPPEDBY", text=provenance))

        # ── iTunes-Metadaten ──
        it = enriched.itunes_data
        if it:
            iid: int | None
            for tag_key, iid, desc in (
                ("TXXX:ITunesTrackId", it.track_id, "ITunesTrackId"),
                ("TXXX:ITunesArtistId", it.artist_id, "ITunesArtistId"),
                ("TXXX:ITunesCollectionId", it.collection_id, "ITunesCollectionId"),
            ):
                if iid is not None:
                    audio.delall(tag_key)
                    audio.add(TXXX(encoding=3, desc=desc, text=str(iid)))

            surl: str | None
            for tag_key, surl, desc in (
                ("TXXX:ITunesTrackUrl", it.track_view_url, "ITunesTrackUrl"),
                ("TXXX:ITunesPreviewUrl", it.preview_url, "ITunesPreviewUrl"),
            ):
                if surl:
                    audio.delall(tag_key)
                    audio.add(TXXX(encoding=3, desc=desc, text=surl))

            icount: int | None
            for tag_key, icount, desc in (
                ("TXXX:ITunesTrackCount", it.track_count, "ITunesTrackCount"),
                ("TXXX:ITunesDiscCount", it.disc_count, "ITunesDiscCount"),
            ):
                if icount is not None:
                    audio.delall(tag_key)
                    audio.add(TXXX(encoding=3, desc=desc, text=str(icount)))

            if it.country:
                audio.delall("TXXX:ITunesCountry")
                audio.add(TXXX(encoding=3, desc="ITunesCountry", text=it.country))
            if it.explicitness:
                audio.delall("TXXX:ITunesExplicitness")
                audio.add(TXXX(encoding=3, desc="ITunesExplicitness", text=it.explicitness))

        # ── Fingerprint ──
        if recording_id:
            audio.delall("TXXX:MusicBrainz Recording Id")
            audio.add(TXXX(encoding=3, desc="MusicBrainz Recording Id", text=recording_id))
        audio.delall("TXXX:AcoustID Score")
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text=str(round(score, 4))))

        # ── Deezer Popularity Rank (wird beim Reconcile aus dem Tag zurückgelesen) ──
        if deezer and deezer.rank is not None:
            audio.delall("TXXX:Deezer Popularity Rank")
            audio.add(TXXX(encoding=3, desc="Deezer Popularity Rank", text=str(deezer.rank)))

        # UPC/EAN des Albums von Deezer
        if deezer and deezer.upc:
            audio.delall("TXXX:UPC")
            audio.add(TXXX(encoding=3, desc="UPC", text=deezer.upc))

        if mb_data is not None:
            mbs: str | None
            for tag_key, mbs, desc in (
                ("TXXX:MusicBrainz Recording Title", mb_data.recording_title, "MusicBrainz Recording Title"),
                ("TXXX:MusicBrainz Recording Artist", mb_data.recording_artist, "MusicBrainz Recording Artist"),
                ("TXXX:MusicBrainz Release Title", mb_data.release_title, "MusicBrainz Release Title"),
                ("TXXX:MusicBrainz Release Date", mb_data.release_date, "MusicBrainz Release Date"),
                (
                    "TXXX:MusicBrainz Album Release Country",
                    mb_data.release_country,
                    "MusicBrainz Album Release Country",
                ),
                ("TXXX:MusicBrainz Release Id", mb_data.release_id, "MusicBrainz Release Id"),
                ("TXXX:MusicBrainz Release Group Type", mb_data.release_group_type, "MusicBrainz Release Group Type"),
                ("TXXX:CatalogNumber", mb_data.release_catalog_no, "CatalogNumber"),
                ("TXXX:Barcode", mb_data.barcode, "Barcode"),
            ):
                if mbs:
                    audio.delall(tag_key)
                    audio.add(TXXX(encoding=3, desc=desc, text=mbs))

            if mb_data.genres:
                audio.delall("TXXX:MusicBrainz Genres")
                audio.add(TXXX(encoding=3, desc="MusicBrainz Genres", text=", ".join(mb_data.genres)))

        # ISRC: deezer → mb_data
        isrc_val = deezer.isrc if deezer else None
        if not isrc_val and mb_data and mb_data.isrcs:
            isrc_val = mb_data.isrcs[0]
        if isrc_val:
            audio.delall("TSRC")
            audio.add(TSRC(encoding=3, text=isrc_val))

        # ── Cover-Artwork ──
        if cover_bytes:
            _embed_apic(audio, cover_bytes, 3, "Cover")
        if artist_image is not None:
            _embed_apic(audio, artist_image, 8, "Performer")

        # ── Liedtexte ──
        if lyrics:
            audio.delall("USLT")
            audio.add(USLT(encoding=1, lang="eng", desc="", text=lyrics))
            audio.delall("TXXX:Lyrics")
            audio.add(TXXX(encoding=1, desc="Lyrics", text=lyrics))


__all__ = ["ID3Tagger", "TrackTagger", "read_acoustid_score"]
