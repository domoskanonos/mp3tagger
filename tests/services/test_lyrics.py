"""Tests for radio_ripper.services.lyrics."""

from __future__ import annotations

from unittest.mock import AsyncMock

from radio_ripper.services.lyrics import LRCLibProvider, _clean_title


class TestCleanTitle:
    def test_strips_feat(self):
        assert _clean_title("Horizont (feat. Johannes Oerding)") == "Horizont"

    def test_strips_ft(self):
        assert _clean_title("Love In This Club ft. Young Jeezy") == "Love In This Club"

    def test_strips_bracketed_vs(self):
        assert _clean_title("Senorita (vs. Justin Bieber)") == "Senorita"


class TestLRCLibProvider:
    async def test_fetch_returns_lyrics_from_exact(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {"plainLyrics": "Hello\nWorld\n", "instrumental": False}
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result == "Hello\nWorld"
        client.get_json.assert_called_once_with(
            "https://lrclib.net/api/get",
            params={"artist_name": "Test", "track_name": "Song"},
            timeout=5.0,
        )

    async def test_fetch_falls_back_to_search(self) -> None:
        client = AsyncMock()
        client.get_json.side_effect = [
            RuntimeError("exact not found"),
            [{"plainLyrics": "Search\nResult\n", "instrumental": False}],
        ]
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result == "Search\nResult"

    async def test_fetch_returns_none_on_instrumental(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {"plainLyrics": "Hello", "instrumental": True}
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_returns_none_on_empty_lyrics(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {"plainLyrics": "", "instrumental": False}
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_returns_none_on_missing_key(self) -> None:
        client = AsyncMock()
        client.get_json.return_value = {"instrumental": False}
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_returns_none_on_exception_both(self) -> None:
        client = AsyncMock()
        client.get_json.side_effect = RuntimeError("API down")
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result is None

    async def test_fetch_empty_artist(self) -> None:
        client = AsyncMock()
        provider = LRCLibProvider(client)
        result = await provider.fetch("", "Song")
        assert result is None
        client.get_json.assert_not_called()

    async def test_search_skips_instrumental_results(self) -> None:
        client = AsyncMock()
        client.get_json.side_effect = [
            RuntimeError("exact miss"),
            [
                {"plainLyrics": "", "instrumental": True},
                {"plainLyrics": "Real\nLyrics\n", "instrumental": False},
            ],
        ]
        provider = LRCLibProvider(client)
        result = await provider.fetch("Test", "Song")
        assert result == "Real\nLyrics"
