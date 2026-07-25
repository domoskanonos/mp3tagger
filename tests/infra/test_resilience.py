"""Tests for radio_ripper.infra.resilience."""

from __future__ import annotations

import pytest

from radio_ripper.infra.resilience import retry_async


class TestRetryAsync:
    async def test_succeeds_first_try(self):
        calls = 0

        @retry_async(max_attempts=3, base_delay=0.001)
        async def fn():
            nonlocal calls
            calls += 1
            return "ok"

        assert await fn() == "ok"
        assert calls == 1

    async def test_retries_then_succeeds(self):
        calls = 0

        @retry_async(max_attempts=3, base_delay=0.001)
        async def fn():
            nonlocal calls
            calls += 1
            if calls < 2:
                raise ValueError("fail")
            return "ok"

        assert await fn() == "ok"
        assert calls == 2

    async def test_exhausts_attempts_raises(self):
        calls = 0

        @retry_async(max_attempts=2, base_delay=0.001)
        async def fn():
            nonlocal calls
            calls += 1
            raise ValueError("always")

        with pytest.raises(ValueError, match="always"):
            await fn()
        assert calls == 2

    async def test_skips_unhandled_exception_types(self):
        @retry_async(max_attempts=5, base_delay=0.001, exceptions=(ValueError,))
        async def fn():
            raise KeyError("nope")

        with pytest.raises(KeyError, match="nope"):
            await fn()

    async def test_on_retry_callback_invoked(self):
        attempts = []

        def on_retry(exc, attempt):
            attempts.append((str(exc), attempt))

        @retry_async(max_attempts=3, base_delay=0.001, on_retry=on_retry)
        async def fn():
            raise ValueError("boom")

        with pytest.raises(ValueError):
            await fn()
        assert len(attempts) == 2
        assert attempts[0][1] == 1

    async def test_max_delay_capped(self):
        sleeps: list[float] = []

        import radio_ripper.infra.resilience as R

        orig_sleep = R.asyncio.sleep

        async def fake_sleep(t):
            sleeps.append(t)

        R.asyncio.sleep = fake_sleep
        try:

            @retry_async(max_attempts=4, base_delay=0.001, max_delay=0.005)
            async def fn():
                raise ValueError("x")

            with pytest.raises(ValueError):
                await fn()
        finally:
            R.asyncio.sleep = orig_sleep
        # Three retry waits (attempts 1,2,3 → 3 sleeps between 4 attempts)
        assert len(sleeps) == 3
        assert sleeps[0] == 0.001
        assert sleeps[1] == 0.002
        assert sleeps[2] == 0.004  # 0.002*2=0.004; still under cap 0.005
