"""Tests for radio_ripper.services.metadata_itunes."""

from __future__ import annotations

import pytest
import respx

from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.metadata_itunes import ITunesMetadataProvider, _strip_parens


@pytest.fixture
def client():
    return HttpxAsyncClient()


_ITUNES_RESPONSE = {
    "results": [
        {
            "artistName": "Adele",
            "trackName": "Hello",
            "collectionName": "25",
            "releaseDate": "2015-11-20T00:00:00Z",
            "primaryGenreName": "Pop",
            "artworkUrl100": "https://example.com/100x100bb.jpg",
        }
    ]
}


class TestITunesMetadataProvider:
    async def test_fetch_returns_enriched_info(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client, metadata_timeout=5.0, cover_timeout=5.0)
        with respx.mock:
            respx.get("https://itunes.apple.com/search").respond(json=_ITUNES_RESPONSE)
            info = await provider.fetch("Adele", "Hello")
        assert info is not None
        assert info.artist == "Adele"
        assert info.title == "Hello"
        assert info.album == "25"
        assert info.year == "2015"
        assert info.genre == "Pop"
        assert info.artwork_url is not None
        assert "600x600" in info.artwork_url
        await client.aclose()

    async def test_fetch_returns_none_on_empty_results(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client)
        with respx.mock:
            respx.get("https://itunes.apple.com/search").respond(json={"results": []})
            info = await provider.fetch("Unknown", "Artist")
        assert info is None
        await client.aclose()

    async def test_fetch_returns_none_on_error(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client)
        with respx.mock:
            respx.get("https://itunes.apple.com/search").respond(status_code=500)
            info = await provider.fetch("Adele", "Hello")
        assert info is None
        await client.aclose()

    async def test_fetch_empty_query_returns_none(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client)
        info = await provider.fetch("", "")
        assert info is None
        await client.aclose()

    async def test_fetch_parens_fallback_finds_track(self, client: HttpxAsyncClient):
        """iTunes-Suche ist exact-token-strict: »I See a Dark(er)ness« → 0 Treffer,
        Fallback mit »I See a Darkness« (ohne Klammern) → 1 Treffer."""
        provider = ITunesMetadataProvider(client, metadata_timeout=5.0, cover_timeout=5.0)
        hit_response = {
            "results": [
                {
                    "artistName": "Acid Pauli",
                    "trackName": "I See a Darkness",
                    "collectionName": "Bar 25",
                    "releaseDate": "2010-01-01T00:00:00Z",
                    "primaryGenreName": "Electronic",
                    "artworkUrl100": "https://example.com/100x100bb.jpg",
                }
            ]
        }
        with respx.mock as mock:
            empty_route = mock.get("https://itunes.apple.com/search").respond(json={"results": []})
            hit_route = mock.get("https://itunes.apple.com/search").respond(json=hit_response)
            info = await provider.fetch("Acid Pauli", "I See a Dark(er)ness")
        assert info is not None
        assert info.artist == "Acid Pauli"
        assert info.title == "I See a Darkness"
        assert info.album == "Bar 25"
        assert empty_route.called
        assert hit_route.called
        await client.aclose()

    async def test_fetch_parens_fallback_not_triggered_on_primary_hit(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client, metadata_timeout=5.0, cover_timeout=5.0)
        hit_response = {
            "results": [
                {
                    "artistName": "Adele",
                    "trackName": "Hello",
                    "artworkUrl100": "https://example.com/100x100bb.jpg",
                }
            ]
        }
        with respx.mock as mock:
            route = mock.get("https://itunes.apple.com/search").respond(json=hit_response)
            info = await provider.fetch("Adele", "Hello")
        assert info is not None
        assert info.title == "Hello"
        assert route.call_count == 1
        await client.aclose()

    def test_strip_parens_removes_parenthesized_content(self):
        assert _strip_parens("I See a Dark(er)ness") == "I See a Darkness"
        assert _strip_parens("Song (feat. X) [Remix]") == "Song"
        assert _strip_parens("No Parens Here") == "No Parens Here"
        assert _strip_parens("Round Brackets (mix) and [brackets]") == "Round Brackets and"
        assert _strip_parens("Word  Multi   Space") == "Word Multi Space"

    async def test_download_image_succeeds(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client)
        with respx.mock:
            respx.get("https://example.com/cover.jpg").respond(content=b"\xff\xd8\xff\x00" * 100)
            data = await provider.download_image("https://example.com/cover.jpg")
        assert data is not None
        assert len(data) > 64
        await client.aclose()

    async def test_download_image_returns_none_on_error(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client)
        with respx.mock:
            respx.get("https://example.com/bad.jpg").respond(status_code=404)
            data = await provider.download_image("https://example.com/bad.jpg")
        assert data is None
        await client.aclose()

    async def test_download_image_returns_none_on_too_small(self, client: HttpxAsyncClient):
        provider = ITunesMetadataProvider(client)
        with respx.mock:
            respx.get("https://example.com/tiny.jpg").respond(content=b"\x00" * 10)
            data = await provider.download_image("https://example.com/tiny.jpg")
        assert data is None
        await client.aclose()

    def test_upgrade_artwork_increases_resolution(self):
        url = "https://example.com/100x100bb.jpg"
        upgraded = ITunesMetadataProvider._upgrade_artwork(url)
        assert "600x600" in upgraded

    def test_upgrade_artwork_from_60(self):
        url = "https://example.com/60x60bb.jpg"
        upgraded = ITunesMetadataProvider._upgrade_artwork(url)
        assert "600x600" in upgraded
