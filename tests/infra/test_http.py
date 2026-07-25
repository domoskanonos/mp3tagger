"""Tests for radio_ripper.infra.http (httpx-backed default impl)."""

from __future__ import annotations

import httpx
import pytest
import respx

from radio_ripper.infra.http import HttpxAsyncClient


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
            respx.get("https://example.com/s").respond(
                content=b"data", headers={"icy-metaint": "16000"}
            )
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
