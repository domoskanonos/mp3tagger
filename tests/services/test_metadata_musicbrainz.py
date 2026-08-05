"""Tests for radio_ripper.services.metadata_musicbrainz."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import respx

from radio_ripper.infra.http import AsyncHttpClient, HttpxAsyncClient
from radio_ripper.services.metadata_musicbrainz import CoverArtArchiveProvider


@pytest.fixture
def client():
    return HttpxAsyncClient()


_MBZ_RECORDING_URL = "https://musicbrainz.org/ws/2/recording/"
_CAA_FRONT_URL = "https://coverartarchive.org/release/"


class _CountingClient(AsyncHttpClient):
    """Zählt get_json-Calls und protokolliert deren Zeitstempel (Rate-Limit-Test)."""

    def __init__(self) -> None:
        self.get_json_times: list[float] = []
        self._calls = 0

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        self._calls += 1
        self.get_json_times.append(time.monotonic())
        if "recording" in url:
            return {"releases": [{"id": "rel-1", "status": "Official"}]}
        return {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        return ""

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        return b""

    def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[bytes]:
        return iter([b""])  # type: ignore[return-value]

    def response_headers(self) -> dict[str, str]:
        return {}

    async def aclose(self) -> None:
        pass


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

    async def test_rate_limit_serialized_under_concurrency(self):
        """Parallele MB-Zugriffe dürfen NICHT im Burst feuern (Rate-Limit 1 req/s).

        Regression: Vor dem Lock übersprangen parallele Tasks den Sleep und
        schickten alle Requests gleichzeitig → MB 429 → Covers gingen verloren.
        """
        client = _CountingClient()
        provider = CoverArtArchiveProvider(client, timeout=5.0)
        await asyncio.gather(*(provider.fetch_recording_data(f"rec-{i}") for i in range(3)))
        await client.aclose()

        times = client.get_json_times
        assert len(times) >= 3
        # Zwischen aufeinanderfolgenden MB-Requests muss mindestens ~1s liegen.
        for a, b in itertools.pairwise(times):
            assert b - a >= 0.9, f"Rate-Limit verletzt: {b - a:.2f}s zwischen Requests"

    async def test_retries_on_transient_mb_error(self):
        """retry_async greift bei transienten MB-HTTP-Fehlern — Cover geht nicht verloren.

        Regression: _rate_limited_json verschluckte die Exception (return None),
        sodass der Decorator nie retryte und ein 503 das Cover kostete.
        """
        client = _CountingClient()
        client._calls = 0
        provider = CoverArtArchiveProvider(client, timeout=5.0)

        async def _flaky_get_json(*args: Any, **kwargs: Any) -> Any:
            client._calls += 1
            if client._calls == 1:
                raise httpx.HTTPStatusError("503", request=httpx.Request("GET", "x"), response=httpx.Response(503))
            return {"releases": [{"id": "rel-1"}]}

        client.get_json = _flaky_get_json
        payload = await provider._rate_limited_json("https://musicbrainz.org/ws/2/recording/rec-1")
        assert payload == {"releases": [{"id": "rel-1"}]}
        assert client._calls == 2, "Retry muss beim 2. Versuch greifen"
        await client.aclose()
