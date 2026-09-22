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
together: either both are incremented (and, on creation or on a key with no
TTL, expiry is set), or -- on any Redis failure -- neither is, and the request
falls through to the fail-open path above. There is no window where one
counter moved and the other didn't.

`amount` is the token/spend hook: callers pass how much this request cost
(defaulting to 1 for a plain per-request count) and the script increments
both counters by that amount in the same atomic step.

Counters use atomic INCRBY so concurrent requests cannot corrupt the count,
and EXPIRE so each counter resets per window -- set the moment a counter is
created (checked via EXISTS inside the script, not by comparing the
post-increment value, so this is correct for any `amount`, not just
amount=1), and on a counter found with no TTL (audit M4), which would otherwise
count forever. A running window is never refreshed. The gate runs after auth
and tenancy, because it needs the tenant and user identity from the request
context.

get_usage() is a separate, read-only path (MGET, no increment) for reporting
current usage without affecting it.

THE TOKEN COUNTERS ARE A SECOND, DISJOINT PAIR. `tokens:tenant:{t}` and
`tokens:user:{t}:{s}` are moved by their own Lua script, share no key with the
`cost:*` counters above, and answer a different question: what was SPENT, not
how many requests were made. Two functions divide the work, and the division is
the design --

  token_preflight()     READS both, before a model call. Denies (429
                        token_budget_exceeded) at or above either limit, and
                        never writes.
  enforce_token_cost()  WRITES both, after a response is in hand. Counts, warns
                        at the ratio, and never denies -- the call it charges
                        for has already been paid to the provider.

Both fail OPEN on a Redis error, for the same reason enforce_cost does.

WHICH TOKEN KEYS is the scope's choice (register item 127): `token_budget`
"live" is the tenant and user pair above; "history" is the one counter
`tokens:history:tenant:{t}`, with its own limit, in both the pre-flight and the
charge. A history judgement never reads or moves the live pair.

EVERY call here runs inside `cost_breaker` (core/breaker.py), so a store that is
down stops being asked once. Nothing above changes: BreakerOpen IS a
redis.RedisError, so a refusal takes the same fail-open branch a timeout does.

This is a real cap only against real Redis; counters must persist across
requests and workers, which in-memory storage cannot do.
"""

from __future__ import annotations

import logging

import redis

# Aliased to match core/redis.py -- one spelling for redis-py's own asyncio
# namespace, so a grep for it finds every module that touches a client.
from redis import asyncio as redis_async

from dodeal_ai.core import metrics
from dodeal_ai.core.breaker import breaker_field, cost_breaker
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import TokenBudgetExceeded
from dodeal_ai.core.redis import get_cost_client

_logger = logging.getLogger("dodeal_ai.cost")


class CostLimitError(Exception):
    """A per-tenant or per-user cost cap was exceeded. Surfaced as 429.
    Reason code is audit-safe and names which cap was hit."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


# Increments both counters by ARGV[1] and sets a counter's expiry to ARGV[2]
# when it is new or has no TTL (audit M4, the shape of _TAKE_PROMPT_SLOTS_SCRIPT).
# One atomic execution: both counters move together, or (on failure) neither.
_INCR_BOTH_SCRIPT = """
local tenant_existed = redis.call('EXISTS', KEYS[1])
local tenant_count = redis.call('INCRBY', KEYS[1], ARGV[1])
if tenant_existed == 0 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end

local user_existed = redis.call('EXISTS', KEYS[2])
local user_count = redis.call('INCRBY', KEYS[2], ARGV[1])
if user_existed == 0 or redis.call('TTL', KEYS[2]) == -1 then
  redis.call('EXPIRE', KEYS[2], ARGV[2])
end

return {tenant_count, user_count}
"""


async def _incr_both_with_window(
    client: redis_async.Redis,
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
        tenant_count, user_count = await cost_breaker().call(
            lambda: _incr_both_with_window(
                client, tenant_key, user_key, amount, settings.cost_window_seconds
            )
        )
    except redis.RedisError as exc:
        # Fail open (money guard, not security guard). Allow, but log loudly so
        # a bypassed cap is always observable and can be alerted on. Structured
        # fields, not an interpolated message -- the same shape as db2's
        # _bypass(): the JSON formatter lifts extra= onto the top level, so a
        # collector can filter on tenant without parsing the message. No
        # request_id: enforce_cost is called from Gate 4 and from workers and
        # never receives one.
        metrics.BYPASSES.labels(event="cost_cap_bypassed").inc()
        _logger.warning(
            "cost_cap_bypassed",
            extra={
                "reason_code": "cost_store_unavailable",
                "tenant": tenant,
                **breaker_field(exc),
            },
        )
        return

    if tenant_count > settings.cost_per_tenant_limit:
        raise CostLimitError("tenant_quota_exceeded")
    if user_count > settings.cost_per_user_limit:
        raise CostLimitError("user_quota_exceeded")


# Gate 4 on the SERVICE chain (register item D1): the tenant counter alone. The
# service token names no person, so there is no user counter to move; the user
# path above and its script are untouched. Same TTL rule as _INCR_BOTH_SCRIPT.
_INCR_TENANT_SCRIPT = """
local existed = redis.call('EXISTS', KEYS[1])
local count = redis.call('INCRBY', KEYS[1], ARGV[1])
if existed == 0 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return {count}
"""


async def enforce_tenant_cost(tenant: str, amount: int = 1) -> None:
    """The service chain's Gate 4: move `cost:tenant:{t}` by `amount` and deny
    over the tenant cap. Fails OPEN on a Redis error, exactly as enforce_cost."""
    settings = get_settings()
    client = get_cost_client()
    tenant_key = f"cost:tenant:{tenant}"

    async def _incr() -> int:
        (count,) = await client.eval(
            _INCR_TENANT_SCRIPT, 1, tenant_key, amount, settings.cost_window_seconds
        )
        return int(count)

    try:
        tenant_count = await cost_breaker().call(_incr)
    except redis.RedisError as exc:
        metrics.BYPASSES.labels(event="cost_cap_bypassed").inc()
        _logger.warning(
            "cost_cap_bypassed",
            extra={
                "reason_code": "cost_store_unavailable",
                "tenant": tenant,
                **breaker_field(exc),
            },
        )
        return

    if tenant_count > settings.cost_per_tenant_limit:
        raise CostLimitError("tenant_quota_exceeded")


# --- the token counters ----------------------------------------------------
#
# A SECOND script, deliberately a separate constant with the same shape rather
# than the request script reused: the two count different quantities on
# disjoint keys, and an edit aimed at one must not silently change the other.
_ADD_TOKENS_SCRIPT = """
local tenant_existed = redis.call('EXISTS', KEYS[1])
local tenant_total = redis.call('INCRBY', KEYS[1], ARGV[1])
if tenant_existed == 0 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end

local user_existed = redis.call('EXISTS', KEYS[2])
local user_total = redis.call('INCRBY', KEYS[2], ARGV[1])
if user_existed == 0 or redis.call('TTL', KEYS[2]) == -1 then
  redis.call('EXPIRE', KEYS[2], ARGV[2])
end

return {tenant_total, user_total}
"""


def _token_keys(tenant: str, subject: str) -> tuple[str, str]:
    """The two token keys. Their own namespace, sharing nothing with `cost:*`:
    "requests made" must never be able to stand in for "tokens spent"."""
    return f"tokens:tenant:{tenant}", f"tokens:user:{tenant}:{subject}"


# The history budget's one counter (register item 127): the tenant's alone, on a
# script of its own so an edit to the live pair cannot move it.
_ADD_HISTORY_TOKENS_SCRIPT = """
local existed = redis.call('EXISTS', KEYS[1])
local total = redis.call('INCRBY', KEYS[1], ARGV[1])
if existed == 0 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return {total}
"""


def _history_token_key(tenant: str) -> str:
    return f"tokens:history:tenant:{tenant}"


def _budgets(scope: TenantScope) -> tuple[tuple[str, str, int], ...]:
    """(name, key, limit) for every token budget this scope is charged to,
    chosen by `scope.token_budget`. A history judgement is charged to the
    tenant's history counter ONLY -- never the live tenant or user pair."""
    settings = get_settings()
    if scope.token_budget == "history":
        return (
            (
                "history",
                _history_token_key(scope.tenant),
                settings.cost_tokens_history_per_tenant_limit,
            ),
        )
    tenant_key, user_key = _token_keys(scope.tenant, scope.subject)
    return (
        ("tenant", tenant_key, settings.cost_tokens_per_tenant_limit),
        ("user", user_key, settings.cost_tokens_per_user_limit),
    )


async def _add_history_tokens_with_window(
    client: redis_async.Redis, key: str, total: int, window: int
) -> tuple[int]:
    """Add `total` to the history counter in one script execution."""
    (history_total,) = await client.eval(
        _ADD_HISTORY_TOKENS_SCRIPT, 1, key, total, window
    )
    return (int(history_total),)


async def _add_tokens_with_window(
    client: redis_async.Redis,
    tenant_key: str,
    user_key: str,
    total: int,
    window: int,
) -> tuple[int, int]:
    """Atomically add `total` to both token counters in one Lua script
    execution (see _ADD_TOKENS_SCRIPT). Returns (tenant_total, user_total)."""
    tenant_total, user_total = await client.eval(
        _ADD_TOKENS_SCRIPT, 2, tenant_key, user_key, total, window
    )
    return int(tenant_total), int(user_total)


def _warn_on_crossing(
    key: str,
    *,
    before: int,
    after: int,
    limit: int,
    ratio: float,
    scope: TenantScope,
) -> None:
    """One WARNING the first time a running total crosses limit * ratio.

    ONCE PER CROSSING, WITH NO EXTRA STATE. The pre-call total is `after` minus
    what this call added, so "was under, is now at or over" is answerable from
    the one number the script already returned -- no second key, no in-process
    flag, and nothing to get wrong when two workers charge at once. The next
    call is already over the threshold and so does not warn again; the window
    expiring resets the counter and the crossing can happen once more.
    """
    threshold = limit * ratio
    if before >= threshold or after < threshold:
        return
    _logger.warning(
        "token_budget_warning",
        extra={
            "reason_code": "token_budget_warning",
            "key": key,
            "warning_ratio": ratio,
            "total": after,
            "limit": limit,
            "tenant": scope.tenant,
            "request_id": scope.request_id,
        },
    )


async def enforce_token_cost(
    scope: TenantScope,
    *,
    input_tokens: int,
    output_tokens: int,
    profile: str,
) -> None:
    """Charge one model response's tokens to the tenant and the user.

    A METER, NOT A GATE. It counts and it never denies: the call it is charging
    for has ALREADY been paid to the provider, so refusing here would throw the
    answer away and bill for it anyway. `token_preflight` is the gate, and it
    runs before anything is spent. Nothing this function does reaches the
    caller as an exception -- a Redis outage is logged and allowed, exactly as
    enforce_cost's is, because a money guard is not a security guard.

    Both counters move together in one atomic script execution, so a failure
    leaves NEITHER moved rather than a tenant charged and a user not. A history
    scope (register item 127) moves its one history counter instead.
    """
    settings = get_settings()
    client = get_cost_client()
    total = input_tokens + output_tokens
    budgets = _budgets(scope)
    keys = [key for _, key, _ in budgets]
    window = settings.cost_window_seconds

    async def _charge() -> tuple[int, ...]:
        if scope.token_budget == "history":
            return await _add_history_tokens_with_window(client, keys[0], total, window)
        return await _add_tokens_with_window(client, keys[0], keys[1], total, window)

    try:
        totals = await cost_breaker().call(_charge)
    except redis.RedisError as exc:
        # Fail open and say so. The tokens were spent whether or not we counted
        # them, so an uncounted charge is a hole in the meter, not in the bill.
        metrics.BYPASSES.labels(event="token_charge_bypassed").inc()
        _logger.warning(
            "token_charge_bypassed",
            extra={
                "reason_code": "token_store_unavailable",
                "tenant": scope.tenant,
                "request_id": scope.request_id,
                **breaker_field(exc),
            },
        )
        return

    # Numbers, ids and a profile NAME -- never a prompt, never a completion.
    # One `<budget>_total` per counter moved: tenant and user, or history.
    _logger.info(
        "tokens_charged",
        extra={
            "tenant": scope.tenant,
            "subject": scope.subject,
            "request_id": scope.request_id,
            "profile": profile,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            **{
                f"{name}_total": after
                for (name, _, _), after in zip(budgets, totals, strict=True)
            },
        },
    )

    ratio = settings.cost_token_warning_ratio
    for (_, key, limit), after in zip(budgets, totals, strict=True):
        _warn_on_crossing(
            key,
            before=after - total,
            after=after,
            limit=limit,
            ratio=ratio,
            scope=scope,
        )


async def token_preflight(scope: TenantScope) -> bool:
    """Refuse a judgement whose tenant or user is already at its token budget,
    BEFORE the first model call is placed.

    Returns True when either total is at or above cost_token_warning_ratio of
    its limit (register item 61): the judgement runs DEGRADED, with no reprompt,
    and one token_budget_degraded line says so. False otherwise, and on a bypass.

    READ-ONLY, always: MGET of the two token keys and nothing else. The charge
    is enforce_token_cost's, after a response is in hand; a pre-flight that
    wrote would charge a judgement that has not happened yet.

    AT OR ABOVE the limit, not over it. The counters are charged after the
    fact, so by the time a total reaches the cap the budget is already gone --
    ">" would grant one more whole judgement past a limit that was reached.

    FAIL OPEN, like every other cost decision: an unreachable store allows the
    judgement and logs `token_preflight_bypassed` on EVERY bypass. The breaker's
    transition lines are the de-duplication; the old once-per-process latch also
    hid a second outage.
    """
    settings = get_settings()
    client = get_cost_client()
    budgets = _budgets(scope)

    try:
        raws = await cost_breaker().call(
            lambda: client.mget(*(key for _, key, _ in budgets))
        )
    except redis.RedisError as exc:
        metrics.BYPASSES.labels(event="token_preflight_bypassed").inc()
        _logger.warning(
            "token_preflight_bypassed",
            extra={
                "reason_code": "token_store_unavailable",
                "tenant": scope.tenant,
                "request_id": scope.request_id,
                **breaker_field(exc),
            },
        )
        return False

    # A key that has never been charged, or whose window expired, reads None.
    totals = [int(raw or 0) for raw in raws]
    for (_, _, limit), total in zip(budgets, totals, strict=True):
        if total >= limit:
            raise TokenBudgetExceeded()

    ratio = settings.cost_token_warning_ratio
    near = [
        name
        for (name, _, limit), total in zip(budgets, totals, strict=True)
        if total >= limit * ratio
    ]
    if not near:
        return False
    _logger.warning(
        "token_budget_degraded",
        extra={
            "reason_code": "token_budget_degraded",
            "budgets": ",".join(near),
            "tenant": scope.tenant,
            "request_id": scope.request_id,
        },
    )
    return True


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
    tenant_raw, user_raw = await cost_breaker().call(
        lambda: client.mget(tenant_key, user_key)
    )
    return int(tenant_raw or 0), int(user_raw or 0)
