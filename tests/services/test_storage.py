"""Tests for radio_ripper.services.file_utils."""

from __future__ import annotations

from radio_ripper.services.file_utils import compute_file_path, remove_empty_parents, sanitize_filename
from radio_ripper.services.tagging import read_acoustid_score


class TestSanitizeFilename:
    def test_strip_illegal_chars(self):
        assert sanitize_filename("A/B:C*D") == "ABCD"

    def test_replace_newlines_with_space(self):
        assert sanitize_filename("foo\r\nbar") == "foo bar"

    def test_collapse_whitespace(self):
        assert sanitize_filename("  foo   bar  ") == "foo bar"

    def test_truncate_long(self):
        long_name = "A" * 300
        result = sanitize_filename(long_name)
        assert len(result) <= 200

    def test_none_returns_unknown(self):
        assert sanitize_filename(None) == "unknown"

    def test_blank_returns_unknown(self):
        assert sanitize_filename("  ") == "unknown"

    def test_after_stripping_illegal_chars_returns_unknown(self):
        assert sanitize_filename('<>:"') == "unknown"


class TestComputeFilePath:
    def test_artist_title(self, tmp_path):
        p = compute_file_path(tmp_path, "Artist", "Song", "fallback")
        assert p.parent == tmp_path / "Artist"
        assert p.name == "Artist - Song.mp3"

    def test_unknown_uses_fallback(self, tmp_path):
        p = compute_file_path(tmp_path, "", "", "Fallback Stream")
        assert p.parent == tmp_path / "Unknown"
        assert "Fallback Stream" in p.name

    def test_album_subfolder(self, tmp_path):
        p = compute_file_path(tmp_path, "A", "B", "x", album="MyAlbum")
        assert p.parent == tmp_path / "A" / "MyAlbum"

    def test_returns_canonical_path_when_exists(self, tmp_path):
        (tmp_path / "Artist").mkdir()
        (tmp_path / "Artist" / "Artist - Song.mp3").write_text("old")
        p = compute_file_path(tmp_path, "Artist", "Song", "x")
        assert p == tmp_path / "Artist" / "Artist - Song.mp3"


class TestReadAcoustidScore:
    def test_returns_none_when_no_tags(self, tmp_path):
        f = tmp_path / "test.mp3"
        f.write_text("data")
        assert read_acoustid_score(f) is None

    def test_returns_score_when_present(self, tmp_path):
        from mutagen.id3 import ID3, TXXX

        f = tmp_path / "test.mp3"
        f.write_text("data")
        audio = ID3()
        audio.add(TXXX(encoding=3, desc="AcoustID Score", text="0.95"))
        audio.save(f, v2_version=3)
        assert read_acoustid_score(f) == 0.95

    def test_returns_none_when_file_missing(self, tmp_path):
        assert read_acoustid_score(tmp_path / "nonextistent.mp3") is None


class TestRemoveEmptyParents:
    def test_removes_empty_dirs(self, tmp_path):
        d = tmp_path / "a" / "b" / "c"
        d.mkdir(parents=True)
        f = d / "song.mp3"
        f.write_text("x")
        f.unlink()
        remove_empty_parents(f, tmp_path)
        assert not d.exists()
