"""Tests for radio_ripper.services.lyrics."""

from __future__ import annotations

from unittest.mock import AsyncMock

from radio_ripper.services.lyrics import LyricsOvhProvider, _clean_title


class TestCleanTitle:
    def test_strips_feat(self):
        assert _clean_title("Horizont (feat. Johannes Oerding)") == "Horizont"

    def test_strips_ft(self):
        assert _clean_title("Love In This Club ft. Young Jeezy") == "Love In This Club"

    def test_strips_bracketed_vs(self):
        assert _clean_title("Senorita (vs. Justin Bieber)") == "Senorita"


class TestLyricsOvhProvider:
    async def test_fetch_returns_lyrics(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {"lyrics": "Hello\nWorld\n"}
        provider = LyricsOvhProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result == "Hello\nWorld"

    async def test_fetch_returns_none_on_empty(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {"lyrics": ""}
        provider = LyricsOvhProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_returns_none_on_missing_key(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {}
        provider = LyricsOvhProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_returns_none_on_exception(self) -> None:
        client = AsyncMock()
        client.get_json.side_effect = RuntimeError("API down")
        provider = LyricsOvhProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_empty_artist(self) -> None:
        client = AsyncMock()
        provider = LyricsOvhProvider(client)
        result = await provider.fetch("", "Song")
        assert result is None
        client.get_json.assert_not_called()
