"""Tests for radio_ripper.services.metadata_musicbrainz."""

from __future__ import annotations

import pytest
import respx

from radio_ripper.infra.http import HttpxAsyncClient
from radio_ripper.services.metadata_musicbrainz import CoverArtArchiveProvider


@pytest.fixture
def client():
    return HttpxAsyncClient()


_MBZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/"
_CAA_FRONT_URL = "https://coverartarchive.org/release/"


class TestCoverArtArchiveProvider:
    async def test_fetch_cover_returns_bytes_on_hit(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        recording_id = "rec-123"
        release_id = "rel-999"
        cover_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 200
        with respx.mock:
            respx.get(f"{_MBZ_RECORDING_URL}{recording_id}", params__contains={"fmt": "json"}).respond(
                json={"releases": [{"id": release_id}]}
            )
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
            respx.get(f"{_MBZ_RECORDING_URL}bad-id", params__contains={"fmt": "json"}).respond(status_code=500)
            result = await provider.fetch_cover_by_recording_id("bad-id")
        assert result is None
        await client.aclose()

    async def test_no_releases_returns_none(self, client: HttpxAsyncClient):
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        with respx.mock:
            respx.get(f"{_MBZ_RECORDING_URL}rec-456", params__contains={"fmt": "json"}).respond(json={"releases": []})
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
