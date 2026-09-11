"""Repo-level test isolation.

Every test builds its own Settings; no test may depend on a local .env or on
another test's cache. CI has neither a .env file nor DODEAL_JWT_SIGNING_KEY in
the environment, so a test that reaches get_settings() without supplying the
key itself passes locally and fails in CI -- or passes only because an earlier
test happened to seed the lru_cache first.
"""

from __future__ import annotations

import pytest

from dodeal_ai.core.breaker import reset_breakers
from dodeal_ai.core.config import get_settings


@pytest.fixture(autouse=True)
def _isolated_settings(monkeypatch):
    """Give every test the required signing key and an empty settings cache.

    Test-only value, never a real secret. The cache is cleared on the way in
    (so no earlier test's Settings leaks forward) and on the way out (so this
    test's Settings does not leak into the next one). Tests that need a
    different value override it with their own monkeypatch.setenv; tests that
    exercise the fail-closed path delete it with monkeypatch.delenv -- the same
    monkeypatch instance backs both, so either wins over this default.

    Deliberately the ONLY env var set here: anything more would recreate the
    problem it fixes, with tests depending on a shared ambient config instead
    of stating what they need.
    """
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-only-signing-key")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _closed_breakers():
    """Start and end every test with both Redis breakers closed, so one test's outage
    cannot refuse the next test's calls."""
    reset_breakers()
    yield
    reset_breakers()
