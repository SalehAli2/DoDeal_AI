"""Cost gate (Gate 4) — per-tenant and per-user quota enforcement.

Counts requests against per-tenant and per-user caps held in Redis. Enforced,
not merely measured: a request over either cap is denied (429).

Failure policy differs from the security gates on purpose:
  - Over budget            -> deny (429). This is the gate doing its job.
  - Redis unreachable      -> ALLOW, and log a warning (fail open). A cost cap
    is a money guard, not a security guard. A short Redis outage must not take
    the whole service down. The bypass is always logged so it is observable.
    (Auth and tenancy fail CLOSED, because their failure risk is a data breach,
    not a bounded, recoverable spend.)

Counters use atomic INCR so concurrent requests cannot corrupt the count, and
EXPIRE so each counter resets per window. The gate runs after auth and tenancy,
because it needs the tenant and user identity from the request context.

This is a real cap only against real Redis; counters must persist across
requests and workers, which in-memory storage cannot do.
"""
from __future__ import annotations

import logging

import redis

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.redis import get_cost_client

_logger = logging.getLogger("dodeal_ai.cost")


class CostLimitError(Exception):
    """A per-tenant or per-user cost cap was exceeded. Surfaced as 429.
    Reason code is audit-safe and names which cap was hit."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


def _incr_with_window(client: redis.Redis, key: str, window: int) -> int:
    """Atomically increment a counter and ensure it has an expiry window.

    INCR is atomic. EXPIRE is set only when the counter is first created (value
    of 1), so the window measures from first use and the key self-clears.
    """
    value = client.incr(key)
    if value == 1:
        client.expire(key, window)
    return value


def enforce_cost(tenant: str, subject: str) -> None:
    """Increment the tenant and user counters and enforce both caps.

    Raises CostLimitError if either cap is exceeded. If Redis is unreachable,
    allows the request and logs a warning (fail open). Returns None when the
    request is permitted.
    """
    settings = get_settings()
    client = get_cost_client()

    tenant_key = f"cost:tenant:{tenant}"
    user_key = f"cost:user:{tenant}:{subject}"

    try:
        tenant_count = _incr_with_window(
            client, tenant_key, settings.cost_window_seconds
        )
        user_count = _incr_with_window(
            client, user_key, settings.cost_window_seconds
        )
    except redis.RedisError:
        # Fail open (money guard, not security guard). Allow, but log loudly so
        # a bypassed cap is always observable and can be alerted on.
        _logger.warning(
            "cost_cap_bypassed reason=cost_store_unavailable tenant=%s", tenant
        )
        return

    if tenant_count > settings.cost_per_tenant_limit:
        raise CostLimitError("tenant_quota_exceeded")
    if user_count > settings.cost_per_user_limit:
        raise CostLimitError("user_quota_exceeded")