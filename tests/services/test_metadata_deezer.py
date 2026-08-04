"""Tests for radio_ripper.services.metadata_deezer."""

from __future__ import annotations

import httpx
import pytest
import respx

from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.metadata_deezer import DEEZER_SEARCH_URL, DeezerMetadataProvider


@pytest.fixture
def client():
    return HttpxAsyncClient()


_DEEZER_RESPONSE = {
    "data": [
        {
            "title": "Hello",
            "duration": 295,
            "isrc": "GBXXX1500001",
            "rank": 500000,
            "album": {"id": 42, "title": "25", "cover_big": "https://example.com/cover.jpg"},
        }
    ]
}


class TestDeezerMetadataProvider:
    async def test_fetch_returns_data(self, client: HttpxAsyncClient):
        provider = DeezerMetadataProvider(client, timeout=5.0)
        with respx.mock:
            respx.get(DEEZER_SEARCH_URL).respond(json=_DEEZER_RESPONSE)
            respx.get("https://api.deezer.com/album/42").respond(json={"label": "XL", "release_date": "2015-11-20"})
            respx.get("https://example.com/cover.jpg").respond(content=b"\xff\xd8\xff" + b"\x00" * 100)
            data = await provider.fetch("Adele", "Hello")
        assert data is not None
        assert data.album == "25"
        assert data.label == "XL"
        assert data.year == "2015"
        assert data.isrc == "GBXXX1500001"
        assert data.cover_bytes is not None
        await client.aclose()

    async def test_fetch_returns_none_on_no_results(self, client: HttpxAsyncClient):
        provider = DeezerMetadataProvider(client, timeout=5.0)
        with respx.mock:
            respx.get(DEEZER_SEARCH_URL).respond(json={"data": []})
            data = await provider.fetch("Unbekannt", "Gar nichts")
        assert data is None
        await client.aclose()

    async def test_fetch_raises_on_network_error(self, client: HttpxAsyncClient):
        """Netzwerkfehler muss weitergegeben werden, damit der Processor die
        Datei NICHT als 'nicht auf Deezer' löscht."""
        provider = DeezerMetadataProvider(client, timeout=5.0)
        with respx.mock:
            respx.get(DEEZER_SEARCH_URL).mock(return_value=httpx.Response(500))
            with pytest.raises(httpx.HTTPStatusError):
                await provider.fetch("Adele", "Hello")
        await client.aclose()
