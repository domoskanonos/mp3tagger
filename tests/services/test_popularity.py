"""Tests for radio_ripper.services.popularity."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

from radio_ripper.infra.http import AsyncHttpClient
from radio_ripper.services.popularity import DeezerPopularityChecker


class _FakeClient(AsyncHttpClient):
    def __init__(self, result: Any = None, *, raise_on_get_json: bool = False) -> None:
        self._result = result
        self._raise_on_get_json = raise_on_get_json

    async def get_json(self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None) -> Any:
        if self._raise_on_get_json:
            raise RuntimeError("API unreachable")
        return self._result

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


class TestDeezerPopularityChecker:
    async def test_get_rank_happy(self):
        client = _FakeClient({"data": [{"rank": 500}]})
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") == 500

    async def test_get_rank_http_exception_returns_none(self):
        client = _FakeClient(raise_on_get_json=True)
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") is None

    async def test_get_rank_no_data_key(self):
        client = _FakeClient({})
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") is None

    async def test_get_rank_empty_data(self):
        client = _FakeClient({"data": []})
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") is None

    async def test_get_rank_missing_rank_key(self):
        client = _FakeClient({"data": [{}]})
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") is None

    async def test_get_rank_not_a_dict(self):
        client = _FakeClient([1, 2, 3])
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") is None

    async def test_get_rank_string_rank_coerced_to_int(self):
        client = _FakeClient({"data": [{"rank": "999"}]})
        checker = DeezerPopularityChecker(client)
        assert await checker.get_rank("Adele", "Hello") == 999

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
