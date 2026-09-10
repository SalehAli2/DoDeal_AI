import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis
from fastapi.testclient import TestClient

from dodeal_ai.core import redis as redis_module
from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.main import app

client = TestClient(app)


def _probe_result(*, pings: bool):
    """A client whose PING answers, or raises the way a dead server does."""
    fake = MagicMock()
    fake.ping = (
        AsyncMock(return_value=True)
        if pings
        else AsyncMock(side_effect=redis.RedisError("down"))
    )
    return fake


def test_health_returns_ok():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_503_when_config_missing(monkeypatch):
    monkeypatch.setattr(
        "dodeal_ai.main.get_settings",
        lambda: (_ for _ in ()).throw(ConfigError("missing config")),
    )
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not ready"


@pytest.mark.parametrize(
    ("cost_pings", "operational_pings", "expected_redis", "expected_operational"),
    [
        (True, True, "ok", "ok"),
        (False, True, "degraded", "ok"),
        (True, False, "ok", "degraded"),
        (False, False, "degraded", "degraded"),
    ],
)
def test_ready_reports_each_connection_independently(
    monkeypatch, cost_pings, operational_pings, expected_redis, expected_operational
):
    """All four combinations, because the two fields are the whole point: a
    healthy cost store and a dead idempotency store must not read as "Redis ok".

    The real probes run -- only the CLIENTS are faked -- so this also proves a
    raising PING cannot escape past /ready and turn a report into a 500."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()

    with (
        patch.object(
            redis_module,
            "get_cost_client",
            return_value=_probe_result(pings=cost_pings),
        ),
        patch.object(
            redis_module,
            "get_operational_client",
            return_value=_probe_result(pings=operational_pings),
        ),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "redis": expected_redis,
        "operational": expected_operational,
    }
    get_settings.cache_clear()


def test_ready_probes_both_connections_concurrently(monkeypatch):
    """Two slow probes cost ONE probe's time, not two.

    The number that matters operationally: an orchestrator's readiness
    `timeoutSeconds` is sized against a probe, and a sequential /ready would
    make a Redis outage cost twice what that deadline was set for -- killing a
    pod that is serving correctly, which is the exact outage the fail-open
    policy exists to prevent. Both PINGs are patched to sleep, and the assertion
    is that the endpoint finishes in well under their sum.
    """
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()

    delay = 0.2

    def _slow_client():
        fake = MagicMock()

        async def _ping():
            await asyncio.sleep(delay)
            return True

        fake.ping = _ping
        return fake

    with (
        patch.object(redis_module, "get_cost_client", side_effect=_slow_client),
        patch.object(redis_module, "get_operational_client", side_effect=_slow_client),
    ):
        started = time.monotonic()
        response = client.get("/ready")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["redis"] == "ok"
    assert response.json()["operational"] == "ok"
    # Generously below the 0.4s a sequential pair would take, and generously
    # above the 0.2s a concurrent one does: this asserts the SHAPE, not a
    # latency budget, so a slow CI runner cannot make it flaky.
    assert elapsed < delay * 1.8
