"""Tests for radio_ripper.services.storage (tag — path utilities)."""

from __future__ import annotations

import warnings
from unittest.mock import MagicMock, patch

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import pydub.audio_segment  # noqa: F401

from radio_ripper.services.storage import (
    compute_file_path,
    remove_empty_parents,
    remux_mp3,
    sanitize_filename,
)


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

    def test_avoid_collision(self, tmp_path):
        (tmp_path / "Artist").mkdir()
        (tmp_path / "Artist" / "Artist - Song.mp3").write_text("old")
        p = compute_file_path(tmp_path, "Artist", "Song", "x")
        assert p.name.startswith("Artist - Song_")
        assert p.name.endswith(".mp3")
        assert str(p.name) != "Artist - Song.mp3"


class TestRemuxMp3:
    @patch("pydub.AudioSegment.from_file", return_value=MagicMock())
    def test_remux_success(self, mock_from_file, tmp_path):
        src = tmp_path / "test.mp3"
        src.write_text("data")
        mock_from_file.return_value = MagicMock()
        remux_mp3(src)
        assert src.exists()

    @patch("pydub.AudioSegment")
    def test_remux_import_error(self, mock_segment, tmp_path):
        src = tmp_path / "test.mp3"
        src.write_text("data")
        remux_mp3(src)
        assert src.exists()


class TestRemoveEmptyParents:
    def test_removes_empty_dirs(self, tmp_path):
        d = tmp_path / "a" / "b" / "c"
        d.mkdir(parents=True)
        f = d / "song.mp3"
        f.write_text("x")
        f.unlink()
        remove_empty_parents(f, tmp_path)
        assert not d.exists()  # c was removed
        # a and b might still exist because rmdir stops at first non-empty
