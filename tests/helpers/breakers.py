"""Open a real breaker with real failures, never by setting its private state."""

from __future__ import annotations

import pytest
import redis

from dodeal_ai.core.breaker import BreakerState, CircuitBreaker
from dodeal_ai.core.config import get_settings


async def trip(breaker: CircuitBreaker) -> None:
    """Drive `breaker` to OPEN with consecutive failures, as a dead store would."""

    async def _dead() -> None:
        raise redis.RedisError("down")

    for _ in range(get_settings().breaker_failure_threshold):
        with pytest.raises(redis.RedisError):
            await breaker.call(_dead)
    assert breaker.state is BreakerState.OPEN
