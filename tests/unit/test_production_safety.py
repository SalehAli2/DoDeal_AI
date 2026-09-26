"""Production's refusals (core/safety.py): under DODEAL_ENVIRONMENT=production
or staging the API and every call worker refuse to start with the demo flag on,
an http backend, the fake STT, no service key, or an LLM or STT base URL that is
not https, naming each by a fixed code; an HS256 service key logs one WARNING in
production; development refuses nothing."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.safety import UnsafeForProduction, check_production_safety
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, STAGE2_QUEUE
from dodeal_ai.workers import calls as calls_worker

# Everything production asks for; each test breaks one thing.
SAFE: dict[str, Any] = {
    "call_demo_allow_local_audio": False,
    "backend_scheme": "https",
    "call_stt_provider": "gemini",
    "service_jwt_signing_key": "a-service-public-key",
}
UNSAFE: dict[str, Any] = {
    "call_demo_allow_local_audio": True,
    "backend_scheme": "http",
    "call_stt_provider": "fake",
    "service_jwt_signing_key": None,
    "llm_base_url": "http://llm.invalid/v1",
    "call_stt_base_url": "http://stt.invalid/v1",
}
_REGISTRY = {"kind": "openai_compatible", "api_key_env": "K", "timeout_seconds": 5}
_PROFILE = {"provider": "openai_compatible", "model": "m", "api_key_env": "K"}


def _settings(environment: str = "production", **changes: Any) -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key="test-key",
        environment=environment,
        **{**SAFE, **changes},
    )


def _safe_env(monkeypatch: pytest.MonkeyPatch, **unsafe: str) -> None:
    """A production process configured safely, but for `unsafe`."""
    env = {
        "DODEAL_JWT_SIGNING_KEY": "test-key",
        "DODEAL_ENVIRONMENT": "production",
        "DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO": "false",
        "DODEAL_BACKEND_SCHEME": "https",
        "DODEAL_CALL_STT_PROVIDER": "gemini",
        "DODEAL_SERVICE_JWT_SIGNING_KEY": "a-service-public-key",
        **unsafe,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"call_demo_allow_local_audio": True}, "demo_local_audio_on"),
        ({"backend_scheme": "http"}, "backend_scheme_http"),
        ({"call_stt_provider": "fake"}, "stt_provider_fake"),
        ({"service_jwt_signing_key": None}, "service_key_missing"),
        ({"llm_base_url": "http://llm.invalid/v1"}, "llm_base_url_http"),
        ({"llm_base_url": "HTTP://llm.invalid/v1"}, "llm_base_url_http"),
        ({"llm_fallback_base_url": "http://llm.invalid/v1"}, "llm_base_url_http"),
        (
            {"llm_providers": {"own": {**_REGISTRY, "base_url": "http://x.invalid"}}},
            "llm_base_url_http",
        ),
        ({"call_stt_base_url": "http://stt.invalid/v1"}, "stt_base_url_http"),
        (
            {
                "call_stt_profiles": {
                    "own": {**_PROFILE, "base_url": "http://x.invalid"}
                }
            },
            "stt_base_url_http",
        ),
    ],
)
def test_each_refusal_stops_a_production_or_staging_start(
    change: dict[str, Any], reason: str, environment: str
) -> None:
    """The guard (9): each alone refuses, in production and staging alike,
    named by its code."""
    with pytest.raises(UnsafeForProduction) as caught:
        check_production_safety(_settings(environment, **change))
    assert caught.value.reasons == (reason,)
    assert str(caught.value) == f"unsafe_for_production:{reason}"


def test_https_and_unset_base_urls_start() -> None:
    """None uses a built-in https endpoint; every https URL passes."""
    check_production_safety(
        _settings(
            llm_base_url="https://llm.invalid/v1",
            llm_fallback_base_url="https://llm.invalid/v1",
            llm_providers={"own": {**_REGISTRY, "base_url": "https://x.invalid"}},
            call_stt_base_url="https://stt.invalid/v1",
            call_stt_profiles={
                "own": {**_PROFILE, "base_url": "https://x.invalid"},
                "public": {**_PROFILE, "provider": "gemini"},
            },
        )
    )


def test_every_refusal_is_named_at_once_and_none_in_development() -> None:
    for environment in ("production", "staging"):
        with pytest.raises(UnsafeForProduction) as caught:
            check_production_safety(_settings(environment, **UNSAFE))
        assert caught.value.reasons == (
            "demo_local_audio_on",
            "backend_scheme_http",
            "stt_provider_fake",
            "service_key_missing",
            "llm_base_url_http",
            "stt_base_url_http",
        )
    check_production_safety(_settings("development", **UNSAFE))
    check_production_safety(_settings())
    assert Settings(_env_file=None, jwt_signing_key="k").environment == "development"


def test_an_hs256_service_key_logs_one_warning_in_production(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A shared secret that can mint: allowed, and said once. RS256 is quiet,
    and so is HS256 outside production."""
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.startup"):
        check_production_safety(_settings(service_jwt_algorithm="HS256"))
        check_production_safety(_settings())
        check_production_safety(_settings("staging", service_jwt_algorithm="HS256"))
    (line,) = [r for r in caplog.records if r.name == "dodeal_ai.startup"]
    assert line.levelno == logging.WARNING
    assert line.__dict__["event"] == "service_key_hs256"
    assert "a-service-public-key" not in caplog.text


def test_the_api_refuses_to_start_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _safe_env(monkeypatch, DODEAL_BACKEND_SCHEME="http")
    with pytest.raises(UnsafeForProduction) as caught, TestClient(app):
        pass
    assert caught.value.reasons == ("backend_scheme_http",)
    _safe_env(monkeypatch)
    with TestClient(app):
        pass
    get_settings.cache_clear()


@pytest.mark.parametrize("environment", ["production", "staging"])
@pytest.mark.parametrize("queue", [NORMAL_QUEUE, STAGE2_QUEUE])
async def test_every_call_worker_refuses_to_start_in_production(
    monkeypatch: pytest.MonkeyPatch, queue: str, environment: str
) -> None:
    """Before any pool, transcriber or template: nothing is left open."""
    _safe_env(
        monkeypatch,
        DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO="true",
        DODEAL_ENVIRONMENT=environment,
    )
    monkeypatch.setattr(calls_worker, "configure_logging", lambda: None)
    built = calls_worker.worker_settings(queue)
    ctx: dict[str, Any] = {}
    with pytest.raises(UnsafeForProduction) as caught:
        await built["on_startup"](ctx)
    assert caught.value.reasons == ("demo_local_audio_on",)
    assert ctx == {}
    get_settings.cache_clear()
