"""Tests for radio_ripper.services.tagging."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest
from mutagen.id3 import ID3

from radio_ripper.domain.models import EnrichedInfo, TrackInfo
from radio_ripper.infra.errors import TaggingError
from radio_ripper.services.tagging import ID3Tagger, _guess_image_mime, _scale_cover


def _write_blank_mp3(path: Path, size: int = 4096) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfb" + b"\x00" * (size - 2))


class TestID3Tagger:
    def test_write_all_basic(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        tagger.write_all(f, track, "Rock@http://x")
        audio = ID3(f)
        assert (f1 := audio.get("TPE1")) is not None and f1.text == ["Adele"]
        assert (f2 := audio.get("TIT2")) is not None and f2.text == ["Hello"]
        # Ohne Album-Quelle wird KEIN Dummy-Album (Track-Titel) geschrieben
        assert audio.get("TALB") is None
        assert (f4 := audio.get("COMM::eng")) is not None and f4.text == ["Recorded via radiostream"]
        assert (f5 := audio.get("TXXX:RIPPEDBY")) is not None and f5.text == ["Rock@http://x"]
        assert (f6 := audio.get("TXXX:AcoustID Score")) is not None and f6.text == ["0.0"]

    def test_write_all_with_enriched(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="Adele - Hello", artist="Adele", title="Hello")
        enriched = EnrichedInfo(artist="Adele", title="Hello", album="25", year="2015", genre="Pop")
        tagger.write_all(f, track, "Rock@url", enriched=enriched)
        audio = ID3(f)
        assert (f1 := audio.get("TALB")) is not None and f1.text == ["25"]
        assert (f2 := audio.get("TDRC")) is not None and str(f2.text[0]) == "2015"
        assert (f3 := audio.get("TCON")) is not None and f3.text == ["Pop"]

    def test_write_all_with_cover(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        cover = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        tagger.write_all(f, track, "S@u", cover_bytes=cover)
        audio = ID3(f)
        apic = audio.get("APIC:Cover")
        assert apic is not None
        assert apic.mime == "image/jpeg"

    def test_write_all_with_fingerprint(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        tagger.write_all(f, track, "S@u", recording_id="abc-123", score=0.9876)
        audio = ID3(f)
        assert (f1 := audio.get("TXXX:MusicBrainz Recording Id")) is not None and f1.text == ["abc-123"]
        assert (f2 := audio.get("TXXX:AcoustID Score")) is not None and f2.text == ["0.9876"]

    def test_write_all_with_lyrics(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo(stream_title="A - B", artist="A", title="B")
        tagger.write_all(f, track, "S@u", lyrics="Hello\nWorld")
        audio = ID3(f)
        assert (f1 := audio.get("USLT::eng")) is not None and f1.text == "Hello\nWorld"
        assert (f2 := audio.get("TXXX:Lyrics")) is not None and f2.text == ["Hello\nWorld"]

    def test_write_all_nonexistent_file_raises(self, tmp_path: Path):
        tagger = ID3Tagger()
        f = tmp_path / "nonexistent_dir" / "song.mp3"
        track = TrackInfo("A - B", "A", "B")
        with pytest.raises(TaggingError):
            tagger.write_all(f, track, "S@u")

    def test_write_all_save_error_raises(self, tmp_path: Path):
        f = tmp_path / "song.mp3"
        _write_blank_mp3(f)
        tagger = ID3Tagger()
        track = TrackInfo("A - B", "A", "B")
        with (
            patch.object(ID3, "save", side_effect=OSError("disk full")),
            pytest.raises(TaggingError, match="Speichern fehlgeschlagen"),
        ):
            tagger.write_all(f, track, "S@u")


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
        assert scaled_bytes == data

    def test_upscale_small_image(self):
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="red")
        buf = io.BytesIO()
        img.save(buf, format="JPEG")
        result = _scale_cover(buf.getvalue())
        assert result is not None
        scaled_bytes, mime = result
        assert mime == "image/jpeg"
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


class TestGuessImageMime:
    def test_jpeg(self):
        assert _guess_image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"

    def test_png(self):
        assert _guess_image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_gif(self):
        assert _guess_image_mime(b"GIF8") == "image/gif"

    def test_defaults_jpeg(self):
        assert _guess_image_mime(b"\x00\x01\x02") == "image/jpeg"
