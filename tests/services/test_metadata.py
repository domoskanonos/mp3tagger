"""Tests for radio_ripper.services.metadata."""

from __future__ import annotations

import pytest
import respx

from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.metadata import (
    CoverArtArchiveProvider,
    ITunesMetadataProvider,
    NullMetadataProvider,
)


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


_MBZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/"
_CAA_FRONT_URL = "https://coverartarchive.org/release/"


class TestCoverArtArchiveProvider:
    async def test_fetch_cover_returns_bytes_on_hit(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        recording_id = "rec-123"
        release_id = "rel-999"
        cover_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        with respx.mock:
            respx.get(
                f"{_MBZ_RECORDING_URL}{recording_id}", params__contains={"fmt": "json"}
            ).respond(json={"releases": [{"id": release_id}]})
            respx.get(f"{_CAA_FRONT_URL}{release_id}/front").respond(content=cover_bytes)
            result = await provider.fetch_cover_by_recording_id(recording_id)
        assert result == cover_bytes
        await client.aclose()

    async def test_empty_recording_id_returns_none(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        result = await provider.fetch_cover_by_recording_id("")
        assert result is None
        await client.aclose()

    async def test_mbz_api_error_returns_none(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        with respx.mock:
            respx.get(f"{_MBZ_RECORDING_URL}bad-id", params__contains={"fmt": "json"}).respond(
                status_code=500
            )
            result = await provider.fetch_cover_by_recording_id("bad-id")
        assert result is None
        await client.aclose()

    async def test_no_releases_returns_none(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        with respx.mock:
            respx.get(f"{_MBZ_RECORDING_URL}rec-456", params__contains={"fmt": "json"}).respond(
                json={"releases": []}
            )
            result = await provider.fetch_cover_by_recording_id("rec-456")
        assert result is None
        await client.aclose()

    async def test_all_caa_404_returns_none(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        release_ids = ["rel-a", "rel-b", "rel-c"]
        with respx.mock:
            respx.get(f"{_MBZ_RECORDING_URL}rec-789", params__contains={"fmt": "json"}).respond(
                json={"releases": [{"id": rid} for rid in release_ids]}
            )
            for rid in release_ids:
                respx.get(f"{_CAA_FRONT_URL}{rid}/front").respond(status_code=404)
            result = await provider.fetch_cover_by_recording_id("rec-789")
        assert result is None
        await client.aclose()


class TestNullMetadataProvider:
    async def test_fetch_returns_none(self):
        p = NullMetadataProvider()
        assert await p.fetch("A", "B") is None

    async def test_download_image_returns_none(self):
        p = NullMetadataProvider()
        assert await p.download_image("http://x") is None
