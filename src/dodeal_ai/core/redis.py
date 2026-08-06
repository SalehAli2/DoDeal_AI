"""Redis connections for the service.

Two named connections are exposed so no caller has to guess which Redis it is
talking to:

  - queue_client: for the Celery-style work queue (used later).
  - cost_client:  for per-tenant and per-user cost/quota counters.

Both point at the local Redis today, on different logical DBs, so their keys
never collide. Connection URLs come from config; real hosts and credentials are
provided by DevOps later with no code change.

Clients are created lazily and cached. Creating a client does not open a socket;
the first command does. A liveness check (ping) is exposed for the readiness
endpoint.
"""
from __future__ import annotations

from functools import lru_cache

import redis

from dodeal_ai.core.config import get_settings


@lru_cache
def get_queue_client() -> redis.Redis:
    """The work-queue connection. Cached so one client is reused."""
    return redis.from_url(
        get_settings().redis_queue_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


@lru_cache
def get_cost_client() -> redis.Redis:
    """The cost/quota connection. Cached so one client is reused."""
    return redis.from_url(
        get_settings().redis_cost_url,
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def check_redis_ready() -> bool:
    """Return True only if both named connections respond to ping. Used by the
    readiness endpoint. Any connection error means not ready (fail closed)."""
    try:
        return bool(get_queue_client().ping()) and bool(get_cost_client().ping())
    except redis.RedisError:
        return False