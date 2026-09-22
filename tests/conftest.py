"""Repo-level test isolation.

Every test builds its own Settings; no test may depend on a local .env or on
another test's cache. CI has neither a .env file nor DODEAL_JWT_SIGNING_KEY in
the environment, so a test that reaches get_settings() without supplying the
key itself passes locally and fails in CI -- or passes only because an earlier
test happened to seed the lru_cache first.

That first sentence was an INTENTION and not a fact until register item 115:
Settings read `.env` on every construction, so the suite was green only for
developers who had not configured one. `_no_dotenv` below makes it true.

Every test also gets the shared Redis fakes, and the default run reaches no
Redis whether or not one is listening (register item 103): `redis_fakes`,
`_closed_redis_urls` and `_real_pool_builds` below.

And no test reads the developer's `.env`, whether or not one exists (register
item 115): `_no_dotenv` below. That is the same promise as 103, for the other
ambient dependency -- see that fixture for why monkeypatching the environment
was not enough.
"""

from __future__ import annotations

import functools
import os
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from dodeal_ai import main
from dodeal_ai.core import redis as redis_module
from dodeal_ai.core import tenant_config
from dodeal_ai.core.breaker import reset_breakers
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.units.structured_intelligence import state
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_operational_redis import FakeOperationalRedis

# Every src module that imports a factory BY NAME, and so holds a reference of
# its own that no patch on core/redis.py would reach. core/redis.py itself stays
# real: its own tests inspect the pool it builds. tests/test_hermetic_fakes.py
# fails if a src module imports a factory by name and is missing here.
_COST_CLIENT_HOLDERS = (limiter, main)
_OPERATIONAL_CLIENT_HOLDERS = (state, main, tenant_config)

# A closed port on loopback. db 1 and db 2 are kept so each URL still names the
# store it stands in for; tests/unit/test_redis.py asserts the two differ and
# that the operational one ends in /2.
_CLOSED_PORT_REDIS_URLS = {
    "DODEAL_REDIS_COST_URL": "redis://127.0.0.1:1/1",
    "DODEAL_REDIS_OPERATIONAL_URL": "redis://127.0.0.1:1/2",
}

_REAL_POOL_BUILDS = pytest.StashKey[list[str]]()

# The tests that build real pools on purpose: core/redis.py IS the factory, and
# the load lane (tests/load/) serves the app on a real Redis. Neither is in the
# default run's reach: test_redis.py opens no socket, and `load` is deselected.
_FACTORY_TESTS = ("tests/unit/test_redis.py::", "tests/load/")

# Lanes that bring their own Redis, so the shared fakes are not installed. The
# redis_real lane never reaches a factory; the load lane reaches the real ones.
_REAL_REDIS_MARKERS = ("redis_real", "load")


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
    quietly run on a fake. Nor is the load lane, which points the real factories
    at its own databases. Both lanes' breakers and caches are still reset.
    """
    redis_module.get_cost_client.cache_clear()
    redis_module.get_operational_client.cache_clear()
    # One test's outage must not refuse the next test's calls.
    reset_breakers()
    fakes = RedisFakes(cost=FakeCostRedis(), operational=FakeOperationalRedis())
    if not any(request.node.get_closest_marker(m) for m in _REAL_REDIS_MARKERS):
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


@pytest.fixture(autouse=True)
def _tenant_config_overrides() -> Iterator[None]:
    """Every test starts with no cached runtime override and with the section
    parsers the lifespan registers (register item 97), which most route tests
    never run. A test's override cannot outlive it in the per-process cache."""
    tenant_config.reset_override_cache()
    tenant_config.register_section_parsers(main.TENANT_CONFIG_SECTIONS)
    yield
    tenant_config.reset_override_cache()


@pytest.fixture(scope="session", autouse=True)
def _no_dotenv() -> Iterator[None]:
    """Cut `.env` out of Settings for the whole run, by clearing `env_file`.

    WHAT IT DOES: Settings declares `env_file=".env"`, resolved against the
    working directory, so every get_settings() in the suite reads whatever the
    developer has configured locally. None means it reads nothing.

    WHY IT IS NOT DONE WITH monkeypatch.setenv: for a DICT field, pydantic-
    settings MERGES sources rather than letting the environment replace the
    file. A test that set DODEAL_DD_API_KEYS by hand still received the `.env`
    entries merged in on top, so no amount of setenv could defend a test -- the
    file has to be out of the sources entirely. This is why a populated `.env`
    turned the suite red (item 115) the first time anybody configured one to
    make a live provider call.

    WHAT A WRONG VALUE DOES: restore the ".env" string here and the suite goes
    green or red depending on a file that is not in the repository, which is
    the defect this closes. A test that genuinely wants a file passes
    `_build_settings(_env_file=...)`, which is untouched and is the correct
    seam. The redis_real lane reads os.environ directly and is unaffected.
    """
    original = Settings.model_config.get("env_file")
    Settings.model_config["env_file"] = None
    try:
        yield
    finally:
        Settings.model_config["env_file"] = original


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
    """Fail the run if any test outside tests/unit/test_redis.py and the load
    lane built a real Redis pool.

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
