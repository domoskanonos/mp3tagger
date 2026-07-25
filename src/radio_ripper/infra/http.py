"""Async HTTP client abstraction.

The :class:`AsyncHttpClient` ABC decouples the rest of the code from the
concrete HTTP library (httpx), enabling easy mocking with ``respx`` or
custom in-memory implementations in tests.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx


class AsyncHttpClient(ABC):
    """Minimal HTTP client surface needed by radio_ripper."""

    @abstractmethod
    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        """Fetch a URL and return its body as text."""

    @abstractmethod
    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        """Fetch a URL and return parsed JSON."""

    @abstractmethod
    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        """Fetch a URL and return raw bytes."""

    @abstractmethod
    def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[bytes]:
        """Stream binary chunks from ``url``.

        Yields ``bytes`` chunks. The response headers (incl. ICY metadata
        interval) are accessible via :attr:`response_headers`.
        """

    @abstractmethod
    def response_headers(self) -> dict[str, str]:
        """Headers of the response from the most recent :meth:`stream_binary` call."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release client resources."""


class HttpxAsyncClient(AsyncHttpClient):
    """Default httpx-backed implementation."""

    def __init__(self, *, user_agent: str = "Radio-Ripper/2.0", verify: bool = True) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            verify=verify,
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        self._last_headers: dict[str, str] = {}

    async def get_text(self, url: str, *, timeout: float | None = None) -> str:
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text

    async def get_json(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: float | None = None
    ) -> Any:
        resp = await self._client.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def get_bytes(self, url: str, *, timeout: float | None = None) -> bytes:
        resp = await self._client.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.content

    async def stream_binary(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> AsyncIterator[bytes]:
        async with self._client.stream(
            "GET",
            url,
            headers=headers,
            timeout=timeout,
        ) as resp:
            resp.raise_for_status()
            self._last_headers = dict(resp.headers)
            async for chunk in resp.aiter_bytes():
                yield chunk

    def response_headers(self) -> dict[str, str]:
        return dict(self._last_headers)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> HttpxAsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


__all__ = ["AsyncHttpClient", "HttpxAsyncClient"]
