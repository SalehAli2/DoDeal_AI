"""Repo-level test isolation.

Every test builds its own Settings; no test may depend on a local .env or on
another test's cache. CI has neither a .env file nor DODEAL_JWT_SIGNING_KEY in
the environment, so a test that reaches get_settings() without supplying the
key itself passes locally and fails in CI -- or passes only because an earlier
test happened to seed the lru_cache first.

Every test also gets the shared Redis fakes, and the default run reaches no
Redis whether or not one is listening (register item 103): `redis_fakes`,
`_closed_redis_urls` and `_real_pool_builds` below.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from dodeal_ai import main
from dodeal_ai.core import redis as redis_module
from dodeal_ai.core.breaker import reset_breakers
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.units.structured_intelligence import state
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_operational_redis import FakeOperationalRedis

# Every src module that imports a factory BY NAME, and so holds a reference of
# its own that no patch on core/redis.py would reach. core/redis.py itself stays
# real: its own tests inspect the pool it builds. tests/test_hermetic_fakes.py
# fails if a src module imports a factory by name and is missing here.
_COST_CLIENT_HOLDERS = (limiter, main)
_OPERATIONAL_CLIENT_HOLDERS = (state, main)

# A closed port on loopback. db 1 and db 2 are kept so each URL still names the
# store it stands in for; tests/unit/test_redis.py asserts the two differ and
# that the operational one ends in /2.
_CLOSED_PORT_REDIS_URLS = {
    "DODEAL_REDIS_COST_URL": "redis://127.0.0.1:1/1",
    "DODEAL_REDIS_OPERATIONAL_URL": "redis://127.0.0.1:1/2",
}

_REAL_POOL_BUILDS = pytest.StashKey[list[str]]()

# The one module whose tests build real pools on purpose: core/redis.py IS the
# factory, and building a pool opens no socket. test_hermetic_fakes.py grants the
# same module the same exemption.
_FACTORY_TESTS = "tests/unit/test_redis.py::"


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Give every test the required signing key and an empty settings cache.

    Test-only value, never a real secret. The cache is cleared on the way in
    (so no earlier test's Settings leaks forward) and on the way out (so this
    test's Settings does not leak into the next one). Tests that need a
    different value override it with their own monkeypatch.setenv; tests that
    exercise the fail-closed path delete it with monkeypatch.delenv -- the same
    monkeypatch instance backs both, so either wins over this default.

    Deliberately the ONLY setting supplied here: anything more would recreate
    the problem it fixes, with tests depending on a shared ambient config
    instead of stating what they need. The two Redis URLs `_closed_redis_urls`
    sets are not config a test can depend on; they point at nothing.
    """
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-only-signing-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@dataclass(frozen=True)
class RedisFakes:
    """The two shared fakes one test's code reaches through every factory name."""

    cost: FakeCostRedis
    operational: FakeOperationalRedis


@pytest.fixture(autouse=True)
def redis_fakes(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[RedisFakes]:
    """Hand every test a fresh FakeCostRedis and FakeOperationalRedis, through
    every name the service's code calls, with both breakers closed.

    WHY. Piece N.4 found the default run was hermetic only on a machine with no
    Redis. Eight modules reached the real, lru_cache'd client: test_vague,
    test_reprompt, test_classification, test_scoring, test_fake_llm and
    test_log_safety through the token charge in complete_once, and
    test_logging_config and test_startup through the lifespan's aclose(). With
    no server the connect failed, the charge failed open, and they passed. With
    the compose Redis up, about 42 failed with "Event loop is closed" and wrote
    tokens:* keys into db1.

    WHY AUTOUSE, NOT EIGHT MODULE PATCHES. Patching the eight one by one would
    leave the ninth module that drives a pass, or opens the app's lifespan, with
    the same bug. A default that every test gets cannot be forgotten.

    A MODULE'S OWN PATCH WINS. A root autouse fixture is set up before any
    fixture a test module defines, whatever order a test lists them in, and
    both patch through the same monkeypatch, so a module's setattr lands second.
    test_hermetic_fakes.py proves it.

    The real factories' caches are cleared on the way in and out, so a client
    core/redis.py's own tests build cannot outlive their event loop.

    The redis_real lane is not covered: it builds its own clients from
    DODEAL_REDIS_REAL_URL, and a lane test that reached a factory must not
    quietly run on a fake. Its breakers and caches are still reset.
    """
    redis_module.get_cost_client.cache_clear()
    redis_module.get_operational_client.cache_clear()
    # One test's outage must not refuse the next test's calls.
    reset_breakers()
    fakes = RedisFakes(cost=FakeCostRedis(), operational=FakeOperationalRedis())
    if request.node.get_closest_marker("redis_real") is None:
        for module in _COST_CLIENT_HOLDERS:
            monkeypatch.setattr(module, "get_cost_client", lambda: fakes.cost)
        for module in _OPERATIONAL_CLIENT_HOLDERS:
            monkeypatch.setattr(
                module, "get_operational_client", lambda: fakes.operational
            )
    yield fakes
    reset_breakers()
    redis_module.get_cost_client.cache_clear()
    redis_module.get_operational_client.cache_clear()


@pytest.fixture(scope="session", autouse=True)
def _closed_redis_urls() -> Iterator[None]:
    """Point both service Redis URLs at a closed loopback port for the whole run.

    The second line of defence, behind `redis_fakes`. A client that somehow
    bypasses the fakes connects to nothing and fails fast, instead of reaching
    a live server. `_real_pool_builds` is what makes that loud: a fail-open path
    would swallow the connection error.

    A test that needs a particular URL still sets its own with monkeypatch. The
    redis_real lane reads DODEAL_REDIS_REAL_URL and neither of these, so it is
    unaffected.
    """
    with pytest.MonkeyPatch.context() as patch:
        for name, url in _CLOSED_PORT_REDIS_URLS.items():
            patch.setenv(name, url)
        yield


@pytest.fixture(scope="session", autouse=True)
def _real_pool_builds(request: pytest.FixtureRequest) -> Iterator[list[str]]:
    """Record every call to core/redis.py's real `_build_pool`, by the test that
    made it.

    Both factories build through it, so any test that reaches a real factory is
    recorded here. pytest_sessionfinish fails the run if one outside
    core/redis.py's own tests did. That is a count checked at the end, not a
    raise: a raise inside a fail-open path would be caught and logged.
    """
    builds: list[str] = []
    request.session.stash[_REAL_POOL_BUILDS] = builds
    real = redis_module._build_pool

    @functools.wraps(real)
    def counted(url: str) -> redis_module.BoundedPool:
        builds.append(os.environ.get("PYTEST_CURRENT_TEST", "outside any test"))
        return real(url)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(redis_module, "_build_pool", counted)
        yield builds


def pytest_sessionfinish(session: pytest.Session) -> None:
    """Fail the run if any test outside tests/unit/test_redis.py built a real
    Redis pool.

    Sets the exit status and names each test rather than raising: a raise here
    is an INTERNALERROR that buries the list.
    """
    reached = sorted(
        {
            build
            for build in session.stash.get(_REAL_POOL_BUILDS, [])
            if not build.startswith(_FACTORY_TESTS)
        }
    )
    if not reached:
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_sep(
            "=", f"{len(reached)} test(s) reached a real Redis client factory", red=True
        )
        for build in reached:
            reporter.write_line(build)
    session.exitstatus = pytest.ExitCode.TESTS_FAILED
