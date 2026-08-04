"""Tests for radio_ripper.services.popularity."""

from __future__ import annotations

from unittest.mock import AsyncMock

from radio_ripper.services.popularity import DeezerPopularityChecker


class TestDeezerPopularityChecker:
    async def test_fetch_artist_image_happy(self):
        client = AsyncMock()
        client.get_json = AsyncMock(return_value={"data": [{"picture_medium": "http://img.jpg"}]})
        client.get_bytes = AsyncMock(return_value=b"image_data")
        checker = DeezerPopularityChecker(client)
        result = await checker.fetch_artist_image("Dr. Dre")
        assert result == b"image_data"

    async def test_fetch_artist_image_no_match(self):
        client = AsyncMock()
        client.get_json = AsyncMock(return_value={"data": []})
        checker = DeezerPopularityChecker(client)
        result = await checker.fetch_artist_image("Unknown")
        assert result is None

    async def test_fetch_artist_image_empty_artist(self):
        client = AsyncMock()
        client.get_json = AsyncMock()
        checker = DeezerPopularityChecker(client)
        result = await checker.fetch_artist_image("")
        assert result is None
        client.get_json.assert_not_called()

    async def test_fetch_artist_image_no_picture_field(self):
        client = AsyncMock()
        client.get_json = AsyncMock(return_value={"data": [{"name": "Dr. Dre"}]})
        checker = DeezerPopularityChecker(client)
        result = await checker.fetch_artist_image("Dr. Dre")
        assert result is None

    async def test_fetch_artist_image_tries_multiple_candidates(self):
        """Erster Kandidat ohne Bild wird übersprungen, zweiter liefert das Bild."""
        client = AsyncMock()
        client.get_json = AsyncMock(
            return_value={"data": [{"name": "Falscher"}, {"name": "Richtiger", "picture_medium": "http://img.jpg"}]}
        )
        client.get_bytes = AsyncMock(return_value=b"image_data")
        checker = DeezerPopularityChecker(client)
        result = await checker.fetch_artist_image("Dr. Dre")
        assert result == b"image_data"

    async def test_fetch_artist_image_prefers_large_picture(self):
        """picture_xl wird vor picture_medium bevorzugt."""
        client = AsyncMock()
        client.get_json = AsyncMock(
            return_value={"data": [{"picture_medium": "http://medium.jpg", "picture_xl": "http://xl.jpg"}]}
        )
        client.get_bytes = AsyncMock(return_value=b"image_data")
        checker = DeezerPopularityChecker(client)
        result = await checker.fetch_artist_image("Dr. Dre")
        assert result == b"image_data"
        client.get_bytes.assert_awaited_once_with("http://xl.jpg", timeout=checker._timeout)
