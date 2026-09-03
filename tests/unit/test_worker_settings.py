"""arq worker skeleton: Redis settings are derived from config, not hardcoded,
and the module holds no tasks yet. Importing it must read no settings and open
no connection -- scripts/verify_wheel.py imports every module in the wheel
inside a venv with no Redis running, and pytest imports this file at COLLECTION,
before the autouse fixture that supplies DODEAL_JWT_SIGNING_KEY can run. An
import that loaded Settings failed the whole file with ConfigError in CI, which
has no .env; an import that dialled out would break the wheel check."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.workers import runner

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_worker_settings_carry_redis_settings(queue_url):
    # Reads the CURRENT settings, not a value captured at class creation: the
    # fixture set the URL long after this module was imported, and the
    # descriptor still sees it.
    assert runner.WorkerSettings.redis_settings.host == "queue.example"


# Imports the module, then asserts the settings cache is still EMPTY. Checking
# the cache rather than only "did the import crash" is what makes this test
# honest on a developer machine: cwd is the repo root (so the package resolves),
# and a repo root usually has a .env, which would have satisfied the eager read
# and let the old code pass here while still failing in CI.
_IMPORT_PROBE = """
import dodeal_ai.workers.runner
from dodeal_ai.core.config import get_settings
assert get_settings.cache_info().currsize == 0, "import built Settings"
"""


def test_import_reads_no_settings():
    """Importing the module must neither build Settings nor need them.

    Runs in a subprocess with every DODEAL_ var stripped -- including the
    signing key, which has no default -- so an eager get_settings() on the
    import path raises ConfigError and the exit code is non-zero. That is the
    CI condition the collection error came from, asserted directly instead of
    left to whether a .env happens to exist.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("DODEAL_")}
    result = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        env=env,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
