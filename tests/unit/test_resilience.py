"""Watchdog: succeeds first try, succeeds on retry, fails closed, respects timeout."""

from __future__ import annotations

import asyncio

import pytest

from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog


@pytest.mark.asyncio
async def test_succeeds_first_try():
    calls = 0

    async def op():
        nonlocal calls
        calls += 1
        return "ok"

    result = await call_with_watchdog(op, label="test")
    assert result == "ok"
    assert calls == 1  # no retry needed


@pytest.mark.asyncio
async def test_succeeds_on_retry():
    calls = 0

    async def flaky():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("first attempt fails")
        return "recovered"

    result = await call_with_watchdog(flaky, label="test", retry=True)
    assert result == "recovered"
    assert calls == 2  # failed once, succeeded on the single retry


@pytest.mark.asyncio
async def test_fails_closed_after_retry():
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    with pytest.raises(ExternalCallError) as exc:
        await call_with_watchdog(always_fails, label="test", retry=True)
    assert exc.value.label == "test"
    assert isinstance(exc.value.cause, RuntimeError)
    assert calls == 2  # tried exactly twice, then failed closed


@pytest.mark.asyncio
async def test_no_retry_tries_once():
    calls = 0

    async def always_fails():
        nonlocal calls
        calls += 1
        raise RuntimeError("down")

    with pytest.raises(ExternalCallError):
        await call_with_watchdog(always_fails, label="test", retry=False)
    assert calls == 1  # retry disabled -> single attempt


@pytest.mark.asyncio
async def test_respects_timeout():
    async def too_slow():
        await asyncio.sleep(1.0)
        return "never"

    with pytest.raises(ExternalCallError) as exc:
        # 0.05s timeout, no retry -> times out fast and fails closed.
        await call_with_watchdog(too_slow, label="test", timeout=0.05, retry=False)
    assert isinstance(exc.value.cause, (asyncio.TimeoutError, TimeoutError))
