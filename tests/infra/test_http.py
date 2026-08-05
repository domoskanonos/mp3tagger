"""Tests for radio_ripper.infra.http (httpx-backed default impl)."""

from __future__ import annotations

import httpx
import pytest
import respx

from radio_ripper.infra.http import HttpxAsyncClient, download_image_or_none


@pytest.fixture
def client():
    return HttpxAsyncClient()


class TestHttpxAsyncClient:
    async def test_get_text(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/foo").respond(text="hello")
            text = await client.get_text("https://example.com/foo")
        assert text == "hello"
        await client.aclose()

    async def test_get_json(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/bar").respond(json={"ok": True})
            data = await client.get_json("https://example.com/bar")
        assert data == {"ok": True}
        await client.aclose()

    async def test_get_bytes(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/raw").respond(content=b"\x00\x01")
            data = await client.get_bytes("https://example.com/raw")
        assert data == b"\x00\x01"
        await client.aclose()

    async def test_stream_binary_returns_chunks(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/stream").respond(content=b"abc")
            chunks = []
            async for chunk in client.stream_binary("https://example.com/stream"):
                chunks.append(chunk)
        assert b"".join(chunks) == b"abc"
        await client.aclose()

    async def test_stream_response_headers_populated(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/s").respond(content=b"data", headers={"icy-metaint": "16000"})
            async for _ in client.stream_binary("https://example.com/s"):
                pass
        assert client.response_headers().get("icy-metaint") == "16000"
        await client.aclose()

    async def test_context_manager(self):
        async with HttpxAsyncClient() as c:
            with respx.mock:
                respx.get("https://example.com/foo").respond(text="hi")
                assert await c.get_text("https://example.com/foo") == "hi"

    async def test_get_text_raises_on_http_error(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/err").respond(status_code=500)
            with pytest.raises(httpx.HTTPStatusError):
                await client.get_text("https://example.com/err")
        await client.aclose()


class TestDownloadImageOrNone:
    async def test_success(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/img.jpg").respond(content=b"\xff\xd8\xff" + b"\x00" * 128)
            data = await download_image_or_none(client, "https://example.com/img.jpg")
        assert data is not None and len(data) > 64
        await client.aclose()

    async def test_retries_on_transient_failure(self, client: HttpxAsyncClient):
        """1. Versuch schlägt fehl (Rate-Limit), Retry liefert das Bild — Cover geht nicht verloren."""
        route = respx.get("https://example.com/flaky.jpg")
        route.side_effect = [httpx.Response(429), httpx.Response(200, content=b"\xff\xd8\xff" + b"\x00" * 128)]
        with respx.mock:
            data = await download_image_or_none(client, "https://example.com/flaky.jpg")
        assert data is not None and len(data) > 64
        await client.aclose()

    async def test_too_small_returns_none(self, client: HttpxAsyncClient):
        with respx.mock:
            respx.get("https://example.com/tiny.jpg").respond(content=b"\x00\x01")
            data = await download_image_or_none(client, "https://example.com/tiny.jpg")
        assert data is None
        await client.aclose()

    async def test_persistent_failure_returns_none(self, client: HttpxAsyncClient):
        route = respx.get("https://example.com/down.jpg")
        route.side_effect = [httpx.Response(500), httpx.Response(500), httpx.Response(500)]
        with respx.mock:
            data = await download_image_or_none(client, "https://example.com/down.jpg")
        assert data is None
        await client.aclose()
