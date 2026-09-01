"""Redis connection for the service.

Named connections are exposed, so no caller has to guess which Redis it is
talking to:

  - cost_client (db1):        per-tenant and per-user cost/quota counters.
  - operational_client (db2): per-request operational state for the feature
                              units -- idempotency reservations, clarification
                              rate limits, per-note attempt counters.

They are separate logical DBs, not namespaces in one, because their failure
policies differ: losing the cost store fails OPEN (a money guard), losing the
idempotency store fails CLOSED (a duplicate-work guard). Keeping them apart
means an outage, a flush or a migration aimed at one cannot silently change
the other's policy.

The work queue has its own connection, owned by arq and configured from
`redis_queue_url` in workers/runner.py -- a bare factory here had no caller, so
it is not duplicated. The two use different logical DBs, so their keys never
collide. The connection URL comes from config; real hosts and credentials are
provided by DevOps later with no code change.

The client is a `redis.asyncio` client (Decision 2 -- one execution model). The
async client raises the same `redis.RedisError` family as the sync one, so
every existing catch keeps its meaning; only the call sites gained an `await`.

The client is created lazily and cached. Creating it does not open a socket and
does not touch an event loop; the first awaited command does. A liveness check
(ping) is exposed for the readiness endpoint.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from redis import asyncio as aioredis

from dodeal_ai.core.config import get_settings


@lru_cache
def get_cost_client() -> aioredis.Redis:
    """The cost/quota connection. Cached so one client is reused."""
    return aioredis.from_url(
        get_settings().redis_cost_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


@lru_cache
def get_operational_client() -> aioredis.Redis:
    """The operational connection (db2): idempotency reservations, rate limits,
    attempt counters. Cached so one client is reused.

    Same shape as get_cost_client, and the 2.0s timeouts are hardcoded here for
    the same reason they are hardcoded there -- they are not yet a tuning
    surface, and audit finding H3 (a Redis outage costing 2.0s per request) is
    addressed at step 3 for both clients at once. Do not add a settings field
    for one of them in isolation; that would make the two drift.
    """
    return aioredis.from_url(
        get_settings().redis_operational_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


async def check_cost_redis_ready() -> bool:
    """Return True if the cost/quota connection responds to ping. Used by the
    readiness endpoint. Only the cost connection is checked -- the queue
    connection has no consumer yet (no worker exists), so pinging it would
    make readiness depend on infrastructure nothing actually uses.

    A connection error returns False; it is the CALLER's decision what that
    means for readiness (see main.py -- Redis down does not fail /ready,
    matching the cost gate's own fail-open policy)."""
    try:
        return bool(await get_cost_client().ping())
    except redis.RedisError:
        return False
