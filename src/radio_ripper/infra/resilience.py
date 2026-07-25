"""Async retry decorator with exponential backoff.

Functions decorated with :func:`retry_async` are retried on exception up to
``max_attempts`` times, with delays that double each time (capped at
``max_delay``). When ``max_attempts`` is exhausted, the final exception is
raised unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")

_logger = logging.getLogger("radio_ripper.resilience")


def retry_async(
    *,
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[BaseException, int], None] | None = None,
) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorate an async function with exponential-backoff retry.

    Args:
        max_attempts: Maximum total attempts (initial + retries).
        base_delay: Initial delay in seconds; doubles each retry.
        max_delay: Upper bound for the delay between attempts.
        exceptions: Exception types that should trigger a retry.
        on_retry: Optional callback ``(exc, attempt)`` invoked before each sleep.

    Returns:
        The decorated async callable.
    """

    def decorator(fn: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            delay = base_delay
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt >= max_attempts:
                        raise
                    if on_retry:
                        on_retry(exc, attempt)
                    else:
                        _logger.debug(
                            "retry %s/%s for %s in %.1fs: %s",
                            attempt,
                            max_attempts - 1,
                            fn.__qualname__,
                            delay,
                            exc,
                        )
                    await asyncio.sleep(delay)
                    delay = min(delay * 2.0, max_delay)
            assert last_exc is not None  # pragma: no cover
            raise last_exc

        return wrapper

    return decorator


__all__ = ["retry_async"]
