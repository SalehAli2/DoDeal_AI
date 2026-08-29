from fastapi.testclient import TestClient

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.main import app

client = TestClient(app)


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


def test_ready_returns_200_degraded_when_redis_down(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()

    async def _redis_down() -> bool:
        return False

    monkeypatch.setattr("dodeal_ai.main.check_cost_redis_ready", _redis_down)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "redis": "degraded"}
    get_settings.cache_clear()


def test_ready_returns_200_ok_when_redis_up(monkeypatch):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    get_settings.cache_clear()

    async def _redis_up() -> bool:
        return True

    monkeypatch.setattr("dodeal_ai.main.check_cost_redis_ready", _redis_up)
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready", "redis": "ok"}
    get_settings.cache_clear()
