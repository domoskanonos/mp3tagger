"""Tests for radio_ripper.services.tagging."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from mutagen.id3 import ID3

from radio_ripper.domain.models import EnrichedInfo, TrackInfo
from radio_ripper.infra.errors import TaggingError
from radio_ripper.services.metadata import MetadataProvider
from radio_ripper.services.tagging import (
    ID3Tagger,
    NullTagger,
    TrackTagger,
    _scale_cover,
    enrich_and_tag,
)


def _write_blank_mp3(path: Path, size: int = 4096) -> None:
    """Write minimal non-empty MP3-like data so ID3() can load it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb" + b"\x00" * (size - 2))


class TestID3Tagger:
    def test_write_basic_tags(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        tagger.write_basic(f, track, "Rock@http://x")
        audio = ID3(f)
        assert audio.get("TPE1").text == ["Adele"]
        assert audio.get("TIT2").text == ["Hello"]
        assert audio.get("COMM::eng").text == ["Recorded via radiostream"]
        assert audio.get("TXXX:RIPPEDBY").text == ["Rock@http://x"]

    def test_write_basic_without_artist(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Jingle", artist="", title="Jingle")
        tagger.write_basic(f, track, "Station@url")
        audio = ID3(f)
        assert "TPE1" not in audio
        assert "TPE2" not in audio
        assert audio.get("TIT2").text == ["Jingle"]

    def test_write_full_tags_with_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        enriched = EnrichedInfo(
            artist="Adele",
            title="Hello",
            album="25",
            year="2015",
            genre="Pop",
        )
        cover = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        tagger.write_full(f, track, enriched, cover, "Rock@url")
        audio = ID3(f)
        assert audio.get("TALB").text == ["25"]
        assert str(audio.get("TDRC").text[0]) == "2015"
        assert audio.get("TCON").text == ["Pop"]
        # TPUB absent when no label was provided
        assert "TPUB" not in audio
        # TRCK absent when no track/disc number
        assert "TRCK" not in audio
        apic = audio.get("APIC:Cover")
        assert apic is not None
        assert apic.mime == "image/jpeg"

    def test_write_full_tags_without_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        enriched = EnrichedInfo(artist="A", title="B", album="alb", year="2020", genre="Pop")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert str(audio.get("TDRC").text[0]) == "2020"
        assert audio.get("TCON").text == ["Pop"]
        # TPUB absent when no label was provided
        assert "TPUB" not in audio
        # TRCK absent when no track/disc number
        assert "TRCK" not in audio
        assert "APIC:Cover" not in audio

    def test_write_full_prefers_enriched_over_track(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Old - Old", artist="Old", title="Old")
        enriched = EnrichedInfo(artist="New", title="NewT")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TPE1").text == ["New"]
        assert audio.get("TIT2").text == ["NewT"]
        assert "TCON" not in audio, "TCON must be absent when genre is not set"

    def test_write_overwrites_previous_tags(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track1 = TrackInfo("A - X", "A", "X")
        tagger.write_basic(f, track1, "S@u")
        track2 = TrackInfo("B - Y", "B", "Y")
        tagger.write_basic(f, track2, "S@u")
        audio = ID3(f)
        assert audio.get("TPE1").text == ["B"]
        assert audio.get("TIT2").text == ["Y"]


class TestGuessMime2:
    def test_gif_cover_is_not_embedded(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        enriched = EnrichedInfo(artist="A", title="B")
        gif_cover = b"GIF89a" + b"\x00" * 100
        tagger.write_full(f, track, enriched, gif_cover, "S@u")
        audio = ID3(f)
        assert "APIC:Cover" not in audio

    def test_fallback_cover_used_when_no_stream_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        enriched = EnrichedInfo(artist="A", title="B")
        fallback = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        tagger.write_full(f, track, enriched, None, "S@u", fallback_cover=fallback)
        audio = ID3(f)
        apic = audio.get("APIC:Cover")
        assert apic is not None
        assert apic.mime == "image/jpeg"

    def test_stream_cover_preferred_over_fallback(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        enriched = EnrichedInfo(artist="A", title="B")
        stream_cover = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        fallback = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        tagger.write_full(f, track, enriched, stream_cover, "S@u", fallback_cover=fallback)
        audio = ID3(f)
        apic = audio.get("APIC:Cover")
        assert apic is not None
        # stream cover takes priority (cover_bytes is used over fallback)


class TestScaleCover:
    def test_gif_returns_none(self):
        result = _scale_cover(b"GIF89a" + b"\x00" * 20)
        assert result is None

    def test_invalid_jpeg_returns_original(self):
        data = b"\xff\xd8\xff\xe0" + b"\x00" * 20
        result = _scale_cover(data)
        assert result is not None
        scaled_bytes, mime = result
        assert mime == "image/jpeg"
        assert scaled_bytes == data  # Pillow decode fails → original returned

    def test_unknown_format_returns_none(self):
        _scale_cover(b"\x00\x01\x02\x03" * 10)
        result2 = _scale_cover(b"\x00\x01\x02\x03" * 10)
        assert result2 is not None

    def test_upscale_small_image(self):
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        result = _scale_cover(buf.getvalue())
        assert result is not None
        scaled_bytes, mime = result
        assert mime == "image/jpeg"
        # Should be upscaled to at least 500px
        reloaded = Image.open(io.BytesIO(scaled_bytes))
        assert max(reloaded.size) >= 500

    def test_downscale_large_image(self):
        from PIL import Image

        img = Image.new("RGB", (2000, 2000), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        result = _scale_cover(buf.getvalue())
        assert result is not None
        scaled_bytes, mime = result
        assert mime == "image/jpeg"
        reloaded = Image.open(io.BytesIO(scaled_bytes))
        assert max(reloaded.size) <= 1000
        assert reloaded.mode == "RGB"

    def test_png_image(self):
        from PIL import Image

        img = Image.new("RGBA", (600, 600), color="green")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result = _scale_cover(buf.getvalue())
        assert result is not None
        scaled_bytes, mime = result
        assert mime == "image/png"
        reloaded = Image.open(io.BytesIO(scaled_bytes))
        assert max(reloaded.size) <= 1000

    def test_mode_conversion_for_jpeg(self):
        from PIL import Image

        from radio_ripper.services.tagging import _guess_image_mime

        img = Image.new("P", (500, 500), color=0)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_data = buf.getvalue()
        assert _guess_image_mime(png_data) == "image/png"
        with patch("radio_ripper.services.tagging._guess_image_mime", return_value="image/jpeg"):
            result = _scale_cover(png_data)
        assert result is not None
        scaled_bytes, mime = result
        assert mime == "image/jpeg"
        reloaded = Image.open(io.BytesIO(scaled_bytes))
        assert reloaded.mode == "RGB"

    def test_import_error_returns_original(self):
        import builtins

        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "PIL":
                raise ImportError("No PIL")
            return original_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", mock_import):
            data = b"\xff\xd8\xff\xe0" + b"\x00" * 20
            result = _scale_cover(data)
            assert result == (data, "image/jpeg")


class TestGuessJpegMime:
    def test_guess_image_mime_jpeg(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"

    def test_guess_image_mime_png(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_guess_image_mime_gif(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"GIF8") == "image/gif"

    def test_guess_image_mime_defaults_jpeg(self):
        from radio_ripper.services.tagging import _guess_image_mime

        assert _guess_image_mime(b"\x00\x01\x02") == "image/jpeg"

    def test_write_to_nonexistent_file_raises_tagging_error(self, tmp_path: Path):
        tagger = ID3Tagger()
        f = tmp_path / "nonexistent_dir" / "song.mp3"
        track = TrackInfo("A - B", "A", "B")
        with pytest.raises(TaggingError):
            tagger.write_basic(f, track, "S@u")

    def test_write_basic_save_error_raises_tagging_error(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        with patch.object(ID3, "save", side_effect=OSError("disk full")):
            with pytest.raises(TaggingError, match="failed to save basic tags"):
                tagger.write_basic(f, track, "S@u")

    def test_write_full_to_nonexistent_file_raises_tagging_error(self, tmp_path: Path):
        tagger = ID3Tagger()
        f = tmp_path / "nonexistent_dir" / "song.mp3"
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A")
        with pytest.raises(TaggingError):
            tagger.write_full(f, track, enriched, None, "S@u")


class TestAlbumFallback:
    """TALB must always be written, falling back to track.title / stream_title."""

    def test_write_basic_writes_talb_from_track_title(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        tagger.write_basic(f, track, "Rock@x")
        audio = ID3(f)
        assert audio.get("TALB").text == ["Hello"]

    def test_write_basic_talb_falls_back_to_stream_title(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Radio Stream", artist="", title="")
        tagger.write_basic(f, track, "S@u")
        audio = ID3(f)
        assert audio.get("TALB").text == ["Radio Stream"]

    def test_write_full_album_uses_enriched_over_track(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        enriched = EnrichedInfo(artist="A", title="B", album="RealAlbum")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TALB").text == ["RealAlbum"]

    def test_write_full_album_falls_back_to_track_title(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Jazz - Mood", artist="Jazz", title="Mood")
        enriched = EnrichedInfo(artist="Jazz", title="Mood", album="")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TALB").text == ["Mood"]


class TestNullTagger:
    def test_write_basic_does_nothing(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        original = f.read_bytes()
        tagger = NullTagger()
        track = TrackInfo("A - B", "A", "B")
        tagger.write_basic(f, track, "S@u")
        assert f.read_bytes() == original

    def test_write_full_does_nothing(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        original = f.read_bytes()
        tagger = NullTagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A")
        tagger.write_full(f, track, enriched, b"cover", "S@u")
        assert f.read_bytes() == original

    def test_update_acoustid_noop(self):
        tagger = NullTagger()
        tagger.update_acoustid(Path("/nonexistent"), "abc", 0.95)
        # should not raise

    def test_embed_cover_noop(self):
        tagger = NullTagger()
        tagger.embed_cover(Path("/nonexistent"), b"cover")
        # should not raise

    def test_update_musicbrainz_metadata_noop(self):
        from radio_ripper.domain.models import MusicBrainzData

        tagger = NullTagger()
        tagger.update_musicbrainz_metadata(Path("/nonexistent"), MusicBrainzData(recording_id="x"))
        # should not raise

    def test_write_lyrics_noop(self):
        tagger = NullTagger()
        tagger.write_lyrics(Path("/nonexistent"), "lyrics")
        # should not raise

    def test_write_artist_image_noop(self):
        tagger = NullTagger()
        tagger.write_artist_image(Path("/nonexistent"), b"img")
        # should not raise


class TestID3TaggerUpdateAcoustid:
    def test_adds_recording_id(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        tagger.update_acoustid(f, "12345", 0.9876)
        audio = ID3(f)
        assert audio.get("TXXX:MusicBrainz Recording Id").text == ["12345"]
        assert audio.get("TXXX:AcoustID Score").text == ["0.9876"]

    def test_omits_recording_id_when_empty(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        tagger.update_acoustid(f, "", 0.5)
        audio = ID3(f)
        assert "TXXX:MusicBrainz Recording Id" not in audio
        assert audio.get("TXXX:AcoustID Score").text == ["0.5"]

    def test_load_error_raises_tagging_error(self):
        tagger = ID3Tagger()
        f = Path("/nonexistent_dir/song.mp3")
        with pytest.raises(TaggingError, match="failed to load .* for acoustid tag"):
            tagger.update_acoustid(f, "id", 0.5)

    def test_save_error_raises_tagging_error(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        with patch.object(ID3, "save", side_effect=OSError("disk full")):
            with pytest.raises(TaggingError, match="failed to save acoustid tags"):
                tagger.update_acoustid(f, "id", 0.5)


class TestID3TaggerEmbedCover:
    def test_embeds_jpeg_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        from PIL import Image

        img = Image.new("RGB", (500, 500), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        tagger.embed_cover(f, buf.getvalue())
        audio = ID3(f)
        apic = audio.get("APIC:Cover")
        assert apic is not None
        assert apic.mime == "image/jpeg"

    def test_embeds_scaled_png_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        from PIL import Image

        img = Image.new("RGBA", (2000, 2000), color="blue")
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        tagger.embed_cover(f, buf.getvalue())
        audio = ID3(f)
        apic = audio.get("APIC:Cover")
        assert apic is not None
        assert apic.mime == "image/png"

    def test_gif_cover_does_not_embed(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        data = b"\xff\xfb" + b"\x00" * 100
        f.write_bytes(data)
        ID3Tagger().embed_cover(f, b"GIF89a" + b"\x00" * 20)
        assert f.read_bytes() == data  # file untouched

    def test_load_error_raises_tagging_error(self):
        tagger = ID3Tagger()
        with pytest.raises(TaggingError, match="failed to load .* for cover embed"):
            tagger.embed_cover(Path("/nonexistent/song.mp3"), b"cover")

    def test_save_error_raises_tagging_error(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        with patch.object(ID3, "save", side_effect=OSError("disk full")):
            with pytest.raises(TaggingError, match="failed to save cover"):
                tagger.embed_cover(f, b"\xff\xd8\xff\xe0" + b"\x00" * 20)


class TestID3TaggerWriteLyrics:
    def test_writes_uslt_frame(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        tagger.write_lyrics(f, "Hello\nWorld")
        audio = ID3(f)
        assert audio.get("USLT::eng").text == "Hello\nWorld"
        assert audio.get("TXXX:Lyrics").text == ["Hello\nWorld"]

    def test_overwrites_previous_lyrics(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        tagger.write_lyrics(f, "Old")
        tagger.write_lyrics(f, "New")
        audio = ID3(f)
        assert audio.get("USLT::eng").text == "New"
        assert audio.get("TXXX:Lyrics").text == ["New"]

    def test_load_error_raises_tagging_error(self):
        tagger = ID3Tagger()
        with pytest.raises(TaggingError, match="failed to load .* for lyrics"):
            tagger.write_lyrics(Path("/nonexistent/song.mp3"), "x")

    def test_save_error_raises_tagging_error(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        with patch.object(ID3, "save", side_effect=OSError("disk full")):
            with pytest.raises(TaggingError, match="failed to save lyrics"):
                tagger.write_lyrics(f, "x")


class TestID3TaggerWriteArtistImage:
    def test_embeds_artist_image(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        img = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        tagger.write_artist_image(f, img)
        audio = ID3(f)
        apic = audio.get("APIC:Performer")
        assert apic is not None
        assert apic.type == 8
        assert apic.desc == "Performer"

    def test_load_error_raises_tagging_error(self):
        tagger = ID3Tagger()
        with pytest.raises(TaggingError, match="failed to load .* for artist image"):
            tagger.write_artist_image(Path("/nonexistent/song.mp3"), b"x")

    def test_save_error_raises_tagging_error(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        with patch.object(ID3, "save", side_effect=OSError("disk full")):
            with pytest.raises(TaggingError, match="failed to save artist image"):
                tagger.write_artist_image(f, b"\xff\xd8\xff\xe0" + b"\x00" * 20)


class TestWriteFullEdgeCases:
    def test_writes_label(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A", title="B", label="MyLabel")
        tagger.write_full(f, track, enriched, None, "Station@u")
        audio = ID3(f)
        assert audio.get("TPUB").text == ["MyLabel"]

    def test_writes_track_number(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A", title="B", track_number=3)
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TRCK").text == ["3"]

    def test_writes_disc_and_track(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A", title="B", track_number=5, disc_number=2)
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TRCK").text == ["2/5"]

    def test_save_error_raises_tagging_error(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A", title="B")
        with patch.object(ID3, "save", side_effect=OSError("disk full")):
            with pytest.raises(TaggingError, match="failed to save enriched tags"):
                tagger.write_full(f, track, enriched, None, "S@u")

    def test_load_error_raises_tagging_error(self, tmp_path: Path):
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A")
        with pytest.raises(TaggingError, match="failed to load"):
            tagger.write_full(tmp_path / "no_dir" / "x.mp3", track, enriched, None, "S@u")

    def test_writes_track_length(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A", title="B", track_length=259720)
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TLEN").text == ["259720"]

    def test_writes_label_only_when_provided(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        enriched = EnrichedInfo(artist="A", title="B", label="LaFace Records")
        tagger.write_full(f, track, enriched, None, "S@u")
        audio = ID3(f)
        assert audio.get("TPUB").text == ["LaFace Records"]


class TestID3TaggerUpdateMusicBrainz:
    def test_writes_mb_metadata(self, tmp_path: Path):
        from radio_ripper.domain.models import MusicBrainzData

        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        mb = MusicBrainzData(
            recording_id="rec-123",
            length_ms=261000,
            isrcs=("USRC10800123",),
            genres=("r&b", "soul"),
            release_id="rel-456",
            release_label="LaFace Records",
            release_catalog_no="88697-23388-2",
            release_date="2008-05-27",
            release_country="US",
            release_group_type="Album",
            barcode="886972338828",
        )
        tagger.update_musicbrainz_metadata(f, mb)
        audio = ID3(f)
        assert audio.get("TPUB").text == ["LaFace Records"]
        assert audio.get("TSRC").text == ["USRC10800123"]
        assert audio.get("TLEN").text == ["261000"]
        assert audio.get("TXXX:MusicBrainz Release Id").text == ["rel-456"]
        assert audio.get("TXXX:MusicBrainz Release Group Type").text == ["Album"]
        assert audio.get("TXXX:MusicBrainz Genres").text == ["r&b, soul"]
        assert audio.get("TXXX:CatalogNumber").text == ["88697-23388-2"]
        assert audio.get("TXXX:Barcode").text == ["886972338828"]

    def test_overwrites_tlen(self, tmp_path: Path):
        from radio_ripper.domain.models import MusicBrainzData

        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        tagger.write_full(
            f, track, EnrichedInfo(artist="A", title="B", track_length=259720), None, "S@u"
        )
        mb = MusicBrainzData(recording_id="x", length_ms=261000)
        tagger.update_musicbrainz_metadata(f, mb)
        audio = ID3(f)
        assert audio.get("TLEN").text == ["261000"]

    def test_load_error_raises_tagging_error(self, tmp_path: Path):
        from radio_ripper.domain.models import MusicBrainzData

        tagger = ID3Tagger()
        mb = MusicBrainzData(recording_id="x")
        with pytest.raises(TaggingError, match="failed to load .* for MB metadata"):
            tagger.update_musicbrainz_metadata(tmp_path / "no_dir" / "x.mp3", mb)


class TestEnrichAndTag:
    @pytest.fixture
    def track(self):
        return TrackInfo(stream_title="Artist - Song", artist="Artist", title="Song")

    @pytest.fixture
    def mock_provider(self):
        provider = AsyncMock(spec=MetadataProvider)
        provider.fetch.return_value = EnrichedInfo(artist="Artist", title="Song", album="Album")
        provider.download_image.return_value = b"\xff\xd8\xff\xe0" + b"\x00" * 50
        return provider

    @pytest.fixture
    def mock_tagger(self):
        return MagicMock(spec=TrackTagger)

    @pytest.mark.asyncio
    async def test_enriches_with_cover(self, mock_provider, mock_tagger, track, tmp_path):
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        result = await enrich_and_tag(mock_provider, mock_tagger, f, track, "S@u")
        assert result is not None
        assert result.album == "Album"
        mock_tagger.write_full.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_info_fallback_cover_embedded(
        self, mock_provider, mock_tagger, track, tmp_path
    ):
        mock_provider.fetch.return_value = None
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        fallback = tmp_path / "cover.jpg"
        fallback.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)
        result = await enrich_and_tag(
            mock_provider,
            mock_tagger,
            f,
            track,
            "S@u",
            fallback_cover_path=fallback,
            embed_cover_art=True,
        )
        assert result is None
        mock_tagger.write_full.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_info_no_fallback(self, mock_provider, mock_tagger, track, tmp_path):
        mock_provider.fetch.return_value = None
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        result = await enrich_and_tag(mock_provider, mock_tagger, f, track, "S@u")
        assert result is None
        mock_tagger.write_full.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_cover_read_error_suppressed(
        self, mock_provider, mock_tagger, track, tmp_path
    ):
        mock_provider.fetch.return_value = None
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        result = await enrich_and_tag(
            mock_provider,
            mock_tagger,
            f,
            track,
            "S@u",
            fallback_cover_path=tmp_path / "nonexistent.jpg",
        )
        assert result is None
        mock_tagger.write_full.assert_not_called()

    @pytest.mark.asyncio
    async def test_enriches_with_artwork_download(
        self, mock_provider, mock_tagger, track, tmp_path
    ):
        mock_provider.fetch.return_value = EnrichedInfo(
            artist="Artist", title="Song", album="Album", artwork_url="http://example.com/cover.jpg"
        )
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        result = await enrich_and_tag(mock_provider, mock_tagger, f, track, "S@u")
        assert result is not None
        mock_provider.download_image.assert_awaited_once_with("http://example.com/cover.jpg")

    @pytest.mark.asyncio
    async def test_write_exception_logged(
        self, mock_provider, mock_tagger, track, tmp_path, caplog
    ):
        import logging

        caplog.set_level(logging.WARNING)
        mock_tagger.write_full.side_effect = OSError("permission denied")
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        result = await enrich_and_tag(
            mock_provider, mock_tagger, f, track, "S@u", logger=logging.getLogger(__name__)
        )
        assert result is not None
        assert "tag-enrichment failed" in caplog.text

    @pytest.mark.asyncio
    async def test_fallback_cover_write_exception_logged(
        self, mock_provider, mock_tagger, track, tmp_path, caplog
    ):
        import logging

        caplog.set_level(logging.WARNING)
        mock_provider.fetch.return_value = None
        mock_tagger.write_full.side_effect = OSError("permission denied")
        f = tmp_path / "s.mp3"
        _write_blank_mp3(f)
        fallback = tmp_path / "cover.jpg"
        fallback.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 50)
        result = await enrich_and_tag(
            mock_provider,
            mock_tagger,
            f,
            track,
            "S@u",
            fallback_cover_path=fallback,
            logger=logging.getLogger(__name__),
        )
        assert result is None
        assert "fallback-cover embed failed" in caplog.text
