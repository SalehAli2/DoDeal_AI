import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import redis
from fastapi.testclient import TestClient

from dodeal_ai.core import redis as redis_module
from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.main import app
from tests.helpers.fake_llm import FakeLLM

client = TestClient(app)


@pytest.fixture
def llm_built(monkeypatch):
    """A client on app.state, the way lifespan leaves one, and a service key.

    TestClient(app) at module scope does NOT run the lifespan, so app.state has
    no `llm` at all by default -- which is the unconfigured state, and /ready is
    strict about it. Every test below that is about REDIS reporting has to say
    the model half is fine first, or it is only ever re-testing the 503. The
    service key is the same kind of precondition since register item D1.
    """
    monkeypatch.setenv("DODEAL_SERVICE_JWT_SIGNING_KEY", "a-service-key")
    app.state.llm = FakeLLM()
    yield
    app.state.llm = None


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
    ("cost_pings", "operational_pings", "jobs_pings", "expected"),
    [
        (True, True, True, ("ok", "ok", "ok")),
        (False, True, True, ("degraded", "ok", "ok")),
        (True, False, True, ("ok", "degraded", "ok")),
        (False, False, True, ("degraded", "degraded", "ok")),
        (True, True, False, ("ok", "ok", "degraded")),
    ],
)
def test_ready_reports_each_connection_independently(
    llm_built,
    monkeypatch,
    cost_pings,
    operational_pings,
    jobs_pings,
    expected,
):
    """Every combination of the first two, and the jobs store (register item 50)
    down on its own: a healthy cost store and a dead idempotency store must not
    read as "Redis ok", and neither may a dead jobs store.

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
        patch.object(
            redis_module,
            "get_jobs_client",
            return_value=_probe_result(pings=jobs_pings),
        ),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "llm": "ok",
        "redis": expected[0],
        "operational": expected[1],
        "jobs": expected[2],
    }
    get_settings.cache_clear()


def test_ready_probes_both_connections_concurrently(llm_built, monkeypatch):
    """Three slow probes cost ONE probe's time, not three.

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
        patch.object(redis_module, "get_jobs_client", side_effect=_slow_client),
    ):
        started = time.monotonic()
        response = client.get("/ready")
        elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["redis"] == "ok"
    assert response.json()["operational"] == "ok"
    assert response.json()["jobs"] == "ok"
    # Generously below the 0.4s a sequential pair would take, and generously
    # above the 0.2s a concurrent one does: this asserts the SHAPE, not a
    # latency budget, so a slow CI runner cannot make it flaky.
    assert elapsed < delay * 1.8


# --- the model half of readiness (item 84) ----------------------------------


def test_ready_is_503_when_no_client_was_built(monkeypatch):
    """THE DEFECT ITEM 84 CLOSES. A pod with no model client can serve /health
    and nothing that matters; answering 200 puts it in rotation to 503 every
    judgement it is then sent. Strict here, deliberately unlike the Redis
    fields, because there is no fail-open path for a missing model."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()
    app.state.llm = None

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not ready", "llm": "not configured"}
    get_settings.cache_clear()


def test_ready_is_503_when_lifespan_never_ran(monkeypatch):
    """No `llm` attribute at all must read as not-ready, not as ready-by-
    omission -- `getattr(..., None)` and not `app.state.llm`, which would
    AttributeError into a 500 and look like a bug rather than a refusal."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()
    if hasattr(app.state, "llm"):
        delattr(app.state, "llm")

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "not ready"
    app.state.llm = None
    get_settings.cache_clear()


def test_ready_reports_llm_ok_once_a_client_is_built(llm_built, monkeypatch):
    """Configured and built: the pod joins rotation and says why it may."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()

    with (
        patch.object(
            redis_module, "get_cost_client", return_value=_probe_result(pings=True)
        ),
        patch.object(
            redis_module,
            "get_operational_client",
            return_value=_probe_result(pings=True),
        ),
        patch.object(
            redis_module, "get_jobs_client", return_value=_probe_result(pings=True)
        ),
    ):
        response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["llm"] == "ok"
    get_settings.cache_clear()


def test_ready_is_503_when_no_service_key_is_set(llm_built, monkeypatch):
    """Register item D1: no service key means every service route 401s, so the
    pod stays out of rotation, and the body names the missing half."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.delenv("DODEAL_SERVICE_JWT_SIGNING_KEY")
    get_settings.cache_clear()

    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not ready", "service_token": "not configured"}
    get_settings.cache_clear()


# --- the served app has no probe route (register item 93) --------------------


def test_create_app_mounts_no_probe_route():
    """A fresh app has the service routes and no /_probe path."""
    from dodeal_ai.main import create_app

    paths = set(create_app().openapi()["paths"])
    assert not [p for p in paths if p.startswith("/_probe")]
    assert {"/health", "/ready", "/api/v1/notes/judgements"} <= paths


def test_the_probe_is_a_404_on_the_served_app():
    """Over HTTP the served app does not answer the probe at all."""
    from fastapi.testclient import TestClient

    from dodeal_ai.main import app as served

    assert TestClient(served).get("/_probe/protected").status_code == 404


def test_the_test_helper_mounts_the_probe_on_its_own_app():
    """The security suite's app has the probe, and is not the served app."""
    from dodeal_ai.main import app as served
    from tests.helpers.probe_app import probe_app

    own = probe_app()
    assert own is not served
    assert "/_probe/protected" in own.openapi()["paths"]
