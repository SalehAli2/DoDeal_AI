"""Cost gate (Gate 4) — per-tenant and per-user quota enforcement.

Counts requests (or, once callers pass a real token/dollar amount, spend)
against per-tenant and per-user caps held in Redis. Enforced, not merely
measured: usage over either cap is denied (429).

Failure policy differs from the security gates on purpose:
  - Over budget            -> deny (429). This is the gate doing its job.
  - Redis unreachable      -> ALLOW, and log a warning (fail open). A cost cap
    is a money guard, not a security guard. A short Redis outage must not take
    the whole service down. The bypass is always logged so it is observable.
    (Auth and tenancy fail CLOSED, because their failure risk is a data breach,
    not a bounded, recoverable spend.)

Atomicity: the tenant and user counters are incremented together in one Redis
Lua script execution (EVAL). Redis runs a script atomically, start to finish,
with no other command interleaving -- so the two counters always move
together: either both are incremented (and, on first creation, expiry is set
on both), or -- on any Redis failure -- neither is, and the request falls
through to the fail-open path above. There is no window where one counter
moved and the other didn't.

`amount` is the token/spend hook: callers pass how much this request cost
(defaulting to 1 for a plain per-request count) and the script increments
both counters by that amount in the same atomic step.

Counters use atomic INCRBY so concurrent requests cannot corrupt the count,
and EXPIRE so each counter resets per window -- set only the moment a counter
is created (checked via EXISTS inside the script, not by comparing the
post-increment value, so this is correct for any `amount`, not just
amount=1). The gate runs after auth and tenancy, because it needs the tenant
and user identity from the request context.

get_usage() is a separate, read-only path (MGET, no increment) for reporting
current usage without affecting it.

This is a real cap only against real Redis; counters must persist across
requests and workers, which in-memory storage cannot do.
"""

from __future__ import annotations

import logging

import redis
from redis import asyncio as aioredis

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.redis import get_cost_client

_logger = logging.getLogger("dodeal_ai.cost")


class CostLimitError(Exception):
    """A per-tenant or per-user cost cap was exceeded. Surfaced as 429.
    Reason code is audit-safe and names which cap was hit."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


# Increments both counters by ARGV[1] and, only for a counter that did not
# exist before this call, sets its expiry to ARGV[2]. Runs as one atomic
# Redis script execution: both counters always move together, or (on
# failure) neither does.
_INCR_BOTH_SCRIPT = """
local tenant_existed = redis.call('EXISTS', KEYS[1])
local tenant_count = redis.call('INCRBY', KEYS[1], ARGV[1])
if tenant_existed == 0 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end

local user_existed = redis.call('EXISTS', KEYS[2])
local user_count = redis.call('INCRBY', KEYS[2], ARGV[1])
if user_existed == 0 then
  redis.call('EXPIRE', KEYS[2], ARGV[2])
end

return {tenant_count, user_count}
"""


async def _incr_both_with_window(
    client: aioredis.Redis,
    tenant_key: str,
    user_key: str,
    amount: int,
    window: int,
) -> tuple[int, int]:
    """Atomically increment both counters by `amount` in one Lua script
    execution (see _INCR_BOTH_SCRIPT). Returns (tenant_count, user_count)."""
    tenant_count, user_count = await client.eval(
        _INCR_BOTH_SCRIPT, 2, tenant_key, user_key, amount, window
    )
    return int(tenant_count), int(user_count)


async def enforce_cost(tenant: str, subject: str, amount: int = 1) -> None:
    """Atomically increment the tenant and user counters by `amount` and
    enforce both caps.

    Raises CostLimitError if either cap is exceeded. If Redis is unreachable,
    allows the request and logs a warning (fail open) -- and because the
    increment is one atomic script execution, a failure here means NEITHER
    counter moved, not a partial update. Returns None when the request is
    permitted.
    """
    settings = get_settings()
    client = get_cost_client()

    tenant_key = f"cost:tenant:{tenant}"
    user_key = f"cost:user:{tenant}:{subject}"

    try:
        tenant_count, user_count = await _incr_both_with_window(
            client, tenant_key, user_key, amount, settings.cost_window_seconds
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


# Module-level, not an lru_cache: tests reset it with
# monkeypatch.setattr(limiter, "_TOKEN_PREFLIGHT_LOGGED", False), and a cache
# would also key on the arguments, which would make "once per process" mean
# "once per tenant".
_TOKEN_PREFLIGHT_LOGGED = False


async def token_preflight(scope: TenantScope) -> None:
    """SEAM[STEP3] -- the pre-flight token-budget read, not implemented yet.

    A NO-OP today. It exists so the pipeline calls it in the right place now,
    and step 3 fills in the body without moving a call site or changing a
    signature.

    WHAT STEP 3 PUTS HERE: a fail-OPEN read of the tenant's remaining token
    budget before a model call is made, so a tenant that is already over budget
    is refused before we spend, not after. Fail open for the same reason
    enforce_cost does -- a cost cap is a money guard, not a security guard.

    WHY NOT get_usage(): that reads the per-REQUEST counters Gate 4 increments.
    A token budget is a different quantity on a different key with a different
    window, and reusing get_usage would silently make "requests made" stand in
    for "tokens spent" -- a number that looks right and is not. Step 3 adds
    enforce_token_cost and its own counters; this seam calls those.

    Async now, though nothing awaits inside: the real body is a redis.asyncio
    read (Decision 2 -- one execution model), and making it sync today would
    force every call site to change when step 3 lands.

    Logs once per process, at WARNING, so that a deployment running with no
    token pre-flight at all is visible in the logs rather than being a fact you
    have to know. Once, not per request: this is a static property of the build,
    and one line per judgement would be noise that trains people to ignore it.
    """
    global _TOKEN_PREFLIGHT_LOGGED
    if not _TOKEN_PREFLIGHT_LOGGED:
        _TOKEN_PREFLIGHT_LOGGED = True
        _logger.warning(
            "token_preflight_bypassed",
            extra={
                "reason_code": "token_preflight_bypassed",
                "tenant": scope.tenant,
                "request_id": scope.request_id,
            },
        )


async def get_usage(tenant: str, subject: str) -> tuple[int, int]:
    """Read-only: current (tenant_count, user_count) for this window, without
    incrementing either counter. 0 for a counter that hasn't been touched yet
    (or has expired). Unlike enforce_cost, this does not fail open -- a Redis
    error here is a reporting failure, not a request-blocking decision, so it
    propagates to the caller rather than being silently swallowed.
    """
    client = get_cost_client()
    tenant_key = f"cost:tenant:{tenant}"
    user_key = f"cost:user:{tenant}:{subject}"
    tenant_raw, user_raw = await client.mget(tenant_key, user_key)
    return int(tenant_raw or 0), int(user_raw or 0)
