"""arq worker skeleton: Redis settings are derived from config, not hardcoded,
and the module holds no tasks yet. Importing it must open no connection --
scripts/verify_wheel.py imports every module in the wheel inside a venv with no
Redis running, so an import that dialled out would break the wheel check."""

from __future__ import annotations

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.workers import runner


@pytest.fixture
def queue_url(monkeypatch):
    """A tenant-neutral URL that differs from the default in every parsed
    field, so a hardcoded default cannot pass these assertions."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_REDIS_QUEUE_URL", "redis://queue.example:6380/7")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_redis_settings_are_derived_from_the_configured_queue_url(queue_url):
    settings = runner.redis_settings()
    assert settings.host == "queue.example"
    assert settings.port == 6380
    assert settings.database == 7


def test_worker_has_no_functions_yet():
    # Empty on purpose: step 14 appends the real tasks. A placeholder task here
    # would pre-decide a layout the sweep job API has not designed.
    assert runner.WorkerSettings.functions == []


def test_worker_settings_carry_redis_settings():
    assert runner.WorkerSettings.redis_settings is not None
