"""The real-Redis lane: behaviours the hermetic lane fakes, run on a live server.

Run with `pytest -m redis_real --no-cov`. pyproject's addopts exclude the marker
from the default run, and the hook below puts it on every test in this directory
by PATH, so a module that forgets its own `pytestmark` still cannot run there.

SKIPPED, NEVER FAILED, without a usable server. DODEAL_REDIS_REAL_URL unset, a
server that does not answer PING, or a URL that selects one of the service's own
databases (db0 to db2) skips every test with a reason naming the variable. The
reason carries an error TYPE and never the URL, which may hold a password.

UNLESS DODEAL_REDIS_REAL_REQUIRED IS SET (register item 106): then each of those
skips is a failure with the same reason. The CI job sets it, because a lane that
skips there has proven nothing and would still go green. A test-only variable.

NO CLIENT FACTORY IS PATCHED. The lane builds its own clients from the URL, so
`get_cost_client` and `get_operational_client` never point at a live server and
tests/test_hermetic_fakes.py has nothing to police here.

ONE EVENT LOOP FOR THE LANE. The client is session-scoped, and a redis.asyncio
connection belongs to the loop that opened it, so every async fixture and test
in this directory runs on pytest-asyncio's session loop (`loop_scope="session"`).

EVERY KEY LIVES UNDER `key_prefix`, a uuid4 per test, and the fixture deletes all
of them with SCAN (never KEYS) on the way out -- then checks that none are left.
"""

from __future__ import annotations

import os
import pathlib
import uuid
from collections.abc import AsyncIterator
from typing import NoReturn

import pytest
import pytest_asyncio
import redis
from redis import asyncio as redis_async

from dodeal_ai.core.config import Settings

URL_VAR = "DODEAL_REDIS_REAL_URL"
# Any non-empty value turns every skip below into a failure; empty reads as
# unset, the same rule URL_VAR follows.
REQUIRED_VAR = "DODEAL_REDIS_REAL_REQUIRED"

_LANE_DIR = pathlib.Path(__file__).parent

# The service's own logical databases: queue 0, cost 1, operational 2
# (docker-compose.yml). The lane refuses them rather than trusting its prefixes.
_SERVICE_DBS = frozenset({0, 1, 2})

# Settings' DEFAULTS, with no environment and no .env read: a local override
# must not change what the lane measures against, and a session fixture runs
# before the root conftest supplies the signing key a validated build needs.
_DEFAULTS = Settings.model_construct()


def _unusable(reason: str, *, allow_module_level: bool = False) -> NoReturn:
    """Skip the lane with `reason`, or fail it with the same reason when
    DODEAL_REDIS_REAL_REQUIRED is set."""
    if os.environ.get(REQUIRED_VAR):
        pytest.fail(reason, pytrace=False)
    pytest.skip(reason, allow_module_level=allow_module_level)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under this directory redis_real, BEFORE `-m` deselects.

    tryfirst: the mark plugin deselects in this same hook, and a marker added
    after it would arrive too late to keep the test out of the default run.
    """
    for item in items:
        if item.path.is_relative_to(_LANE_DIR):
            item.add_marker(pytest.mark.redis_real)


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def real_redis() -> AsyncIterator[redis_async.Redis]:
    """One client for the lane, from DODEAL_REDIS_REAL_URL, pinged once.

    The two socket timeouts are Settings' defaults, so a URL pointing at nothing
    skips in a quarter-second instead of hanging the run.
    """
    url = os.environ.get(URL_VAR)
    if not url:
        _unusable(
            "DODEAL_REDIS_REAL_URL not set; the real-Redis lane needs a live server",
            allow_module_level=True,
        )

    client = redis_async.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_DEFAULTS.redis_connect_timeout_seconds,
        socket_timeout=_DEFAULTS.redis_socket_timeout_seconds,
    )
    db = int(client.connection_pool.connection_kwargs.get("db", 0))
    if db in _SERVICE_DBS:
        await client.aclose()
        _unusable(
            f"{URL_VAR} selects db{db}; the real-Redis lane refuses db0 to db2, "
            "the service's own stores"
        )
    try:
        await client.ping()
    except redis.RedisError as exc:
        await client.aclose()
        _unusable(
            f"{URL_VAR} is set but PING failed ({type(exc).__name__}); "
            "the real-Redis lane needs a live server"
        )

    yield client
    await client.aclose()


@pytest.fixture(scope="session")
def real_redis_url(real_redis: redis_async.Redis) -> str:
    """The lane's URL, for a test that builds its own pool. Depends on
    `real_redis`, so it skips for exactly the same reasons."""
    return os.environ[URL_VAR]


@pytest_asyncio.fixture(loop_scope="session")
async def key_prefix(real_redis: redis_async.Redis) -> AsyncIterator[str]:
    """A prefix no other test shares; every key under it is gone afterwards."""
    prefix = f"dodeal-real-lane:{uuid.uuid4().hex}:"
    yield prefix

    doomed = [key async for key in real_redis.scan_iter(match=f"{prefix}*")]
    if doomed:
        await real_redis.delete(*doomed)
    left = [key async for key in real_redis.scan_iter(match=f"{prefix}*")]
    assert not left, f"the real-Redis lane left {len(left)} key(s) behind"
