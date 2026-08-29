"""Redis connection for the service.

One named connection is exposed, so no caller has to guess which Redis it is
talking to:

  - cost_client: for per-tenant and per-user cost/quota counters.

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
