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

Every timeout and every pool bound comes from Settings (audit H3). There is no
numeric literal in this module, and a test fails the build if one appears: a
number here would be a second source of truth for a value the deployment is
supposed to own.

The client is created lazily and cached. Creating it does not open a socket and
does not touch an event loop; the first awaited command does. A liveness check
(ping) is exposed per connection for the readiness endpoint.
"""

from __future__ import annotations

from functools import lru_cache

import redis
from redis import asyncio as aioredis

from dodeal_ai.core.config import get_settings


def _build_pool(url: str) -> aioredis.BlockingConnectionPool:
    """A bounded pool for `url`, sized entirely from Settings.

    BlockingConnectionPool, not ConnectionPool: at the cap the plain pool RAISES
    immediately, while this one waits up to `redis_pool_acquire_timeout_seconds`
    and only then refuses -- a brief burst queues instead of erroring, and a
    sustained one still fails fast rather than blocking forever.

    Constructing a pool opens no socket, so this is safe to call at import-time
    depth and in tests with no Redis running.
    """
    settings = get_settings()
    return aioredis.BlockingConnectionPool.from_url(
        url,
        decode_responses=True,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_acquire_timeout_seconds,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )


@lru_cache
def get_cost_client() -> aioredis.Redis:
    """The cost/quota connection (db1). Cached so one client -- and so one pool
    -- is reused; a per-call pool would make the bound meaningless."""
    return aioredis.Redis(connection_pool=_build_pool(get_settings().redis_cost_url))


@lru_cache
def get_operational_client() -> aioredis.Redis:
    """The operational connection (db2): idempotency reservations, rate limits,
    attempt counters. Cached so one client is reused.

    Same shape as get_cost_client and the same four settings, deliberately: the
    two connections are separate so a flush or an outage cannot cross between
    them, not so they can be tuned apart. A per-connection budget would be a new
    decision, and nothing has asked for one.
    """
    return aioredis.Redis(
        connection_pool=_build_pool(get_settings().redis_operational_url)
    )


async def _ping(client: aioredis.Redis) -> bool:
    """True if `client` answers PING within the configured socket timeout. A
    connection error is False, never an exception: readiness is the CALLER's
    decision, and a probe that raised would take /ready down with the
    dependency it is only reporting on."""
    try:
        return bool(await client.ping())
    except redis.RedisError:
        return False


async def check_cost_redis_ready() -> bool:
    """Return True if the cost/quota connection (db1) responds to ping.

    A connection error returns False; it is the CALLER's decision what that
    means for readiness (see main.py -- Redis down does not fail /ready,
    matching the cost gate's own fail-open policy)."""
    return await _ping(get_cost_client())


async def check_operational_redis_ready() -> bool:
    """Return True if the operational connection (db2) responds to ping.

    Reported separately from db1 rather than folded into one flag: the two have
    different failure policies, so "Redis is fine" is not a fact about this
    service -- a healthy cost store and a dead idempotency store is a real and
    materially different state, and one flag would hide it. The queue connection
    is still not probed: it is arq's, and no worker exists to consume it."""
    return await _ping(get_operational_client())
