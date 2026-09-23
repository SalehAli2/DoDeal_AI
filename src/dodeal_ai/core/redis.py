"""Redis connection for the service.

Named connections are exposed, so no caller has to guess which Redis it is
talking to:

  - cost_client (db1):        per-tenant and per-user cost/quota counters.
  - operational_client (db2): per-request operational state for the feature
                              units -- idempotency reservations, clarification
                              rate limits, per-note attempt counters.
  - jobs_client (db3):        Unit B's call jobs and their results (core/jobs.py,
                              register item 50). Fails CLOSED, like db2, but
                              its keys live for days, not for one request.

They are separate logical DBs, not namespaces in one, because their failure
policies differ: losing the cost store fails OPEN (a money guard), losing the
idempotency store fails CLOSED (a duplicate-work guard). Keeping them apart
means an outage, a flush or a migration aimed at one cannot silently change
the other's policy.

The work queue (db0) is arq's. The worker reads it through arq's own settings
in workers/calls.py; the call-job route (register item 50) PUSHES to it through
get_queue_client below, an ArqRedis on the same bounded pool shape, undecoded
because arq stores pickled job bodies. The DBs differ, so no keys collide. The connection URL comes from config; real hosts and credentials are
provided by DevOps later with no code change.

The client is a `redis.asyncio` client (Decision 2 -- one execution model). The
async client raises the same `redis.RedisError` family as the sync one, so
every existing catch keeps its meaning; only the call sites gained an `await`.

Every timeout and every pool bound comes from Settings (audit H3). There is no
numeric literal in this module, and a test fails the build if one appears: a
number here would be a second source of truth for a value the deployment is
supposed to own.

A pool that cannot hand out a connection in time raises PoolExhausted, a
ConnectionError the breaker does not count (register item 81): our own load is
not evidence that Redis is down.

The client is created lazily and cached. Creating it does not open a socket and
does not touch an event loop; the first awaited command does. A liveness check
(ping) is exposed per connection for the readiness endpoint.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import redis
from arq.connections import ArqRedis

# Aliased for readability. NOT the separate, archived PyPI package whose
# name this used to borrow -- this is redis-py's own asyncio namespace.
from redis import asyncio as redis_async
from redis.asyncio.connection import AbstractConnection

from dodeal_ai.core.config import get_settings


class PoolExhausted(redis.ConnectionError):
    """No free connection in the pool within `redis_pool_acquire_timeout_seconds`.

    A ConnectionError, so every existing RedisError handler keeps its meaning:
    the reservation still 503s and the money guards still bypass. Only the
    breaker's COUNT differs (core/breaker.py). The store was never asked, so
    this is news about our own load and not about Redis, and a burst of them
    must not open a breaker on a healthy store (register item 81).

    The message is fixed, so no command, key or value can reach it.
    """

    def __init__(self) -> None:
        super().__init__("redis connection pool exhausted")


class BoundedPool(redis_async.BlockingConnectionPool):
    """A BlockingConnectionPool whose acquire timeout raises PoolExhausted.

    ONLY THE ACQUIRE is relabelled. redis-py refuses an acquire with a
    ConnectionError chained from the wait's own TimeoutError. A socket that will
    not connect fails AFTER the acquire, without that cause, and passes through
    unchanged -- that one IS evidence about the store, and the breaker counts it.
    """

    async def get_connection(self, *args: Any, **kwargs: Any) -> AbstractConnection:
        try:
            return await super().get_connection(*args, **kwargs)
        except redis.ConnectionError as exc:
            if isinstance(exc.__cause__, TimeoutError):
                raise PoolExhausted() from exc
            raise


def _build_pool(url: str, *, decode_responses: bool = True) -> BoundedPool:
    """A bounded pool for `url`, sized entirely from Settings.

    Blocking, not a plain ConnectionPool: at the cap the plain pool RAISES
    immediately, while this one waits up to `redis_pool_acquire_timeout_seconds`
    and only then refuses -- a brief burst queues instead of erroring, and a
    sustained one still fails fast rather than blocking forever.

    Sized by `redis_pool_size`, which by default is one connection per request
    the in-flight cap admits plus headroom (core/config.py): a pool smaller than
    the requests that may hold a connection turns ordinary load into refusals.

    Constructing a pool opens no socket, so this is safe to call at import-time
    depth and in tests with no Redis running.
    """
    settings = get_settings()
    return BoundedPool.from_url(
        url,
        decode_responses=decode_responses,
        max_connections=settings.redis_pool_size,
        timeout=settings.redis_pool_acquire_timeout_seconds,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
    )


@lru_cache
def get_cost_client() -> redis_async.Redis:
    """The cost/quota connection (db1). Cached so one client -- and so one pool
    -- is reused; a per-call pool would make the bound meaningless."""
    return redis_async.Redis(connection_pool=_build_pool(get_settings().redis_cost_url))


@lru_cache
def get_operational_client() -> redis_async.Redis:
    """The operational connection (db2): idempotency reservations, rate limits,
    attempt counters. Cached so one client is reused.

    Same shape as get_cost_client and the same four settings, deliberately: the
    two connections are separate so a flush or an outage cannot cross between
    them, not so they can be tuned apart. A per-connection budget would be a new
    decision, and nothing has asked for one.
    """
    return redis_async.Redis(
        connection_pool=_build_pool(get_settings().redis_operational_url)
    )


@lru_cache
def get_jobs_client() -> redis_async.Redis:
    """The jobs connection (db3): call jobs, their index by call and their
    results. Same shape and settings as the other two, on its own pool, so a
    burst of job polling can never take db2's reservations' connections."""
    return redis_async.Redis(connection_pool=_build_pool(get_settings().redis_jobs_url))


@lru_cache
def get_queue_client() -> ArqRedis:
    """The queue connection (db0) as the API pushes to it: an ArqRedis, so a
    job lands in arq's own format, on a bounded pool of its own. Not decoded:
    arq's job bodies are bytes."""
    return ArqRedis(
        connection_pool=_build_pool(
            get_settings().redis_queue_url, decode_responses=False
        )
    )


async def _ping(client: redis_async.Redis) -> bool:
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


async def check_jobs_redis_ready() -> bool:
    """Return True if the jobs connection (db3) responds to ping. Reported on
    its own: a dead jobs store refuses every call push and poll (503) while
    notes are judged normally, and one flag would hide which half is down."""
    return await _ping(get_jobs_client())
