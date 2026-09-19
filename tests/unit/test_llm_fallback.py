"""Register item 21: the fallback provider is validated like the primary and
answers once, only when the primary returned no response body."""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from dodeal_ai.core.config import ConfigError, Settings, _build_settings
from dodeal_ai.core.llm import (
    FallbackLLMClient,
    LLMErrorReason,
    OpenAICompatibleClient,
    OpenAICompatibleError,
    build_llm_client,
)
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_CLASSIFY
from dodeal_ai.core.prompting import AssembledPrompt

PRIMARY_HOST = "api.groq.com"
FALLBACK_HOST = "api.openai.com"
PRIMARY_KEY = "primary-test-key"
FALLBACK_KEY = "fallback-test-key"
PROMPT = AssembledPrompt(stable="STABLE", variable="VARIABLE")


def _env(monkeypatch: pytest.MonkeyPatch, **fallback: str) -> Settings:
    """A configured groq primary, plus whichever fallback fields the case sets."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-only-key")
    monkeypatch.setenv("DODEAL_LLM_PROVIDER", "groq")
    monkeypatch.setenv("DODEAL_LLM_MODEL", "primary-model")
    monkeypatch.setenv("DODEAL_LLM_API_KEY", PRIMARY_KEY)
    monkeypatch.setenv("DODEAL_BREAKER_FAILURE_THRESHOLD", "1")
    for key in ("PROVIDER", "MODEL", "API_KEY", "BASE_URL"):
        monkeypatch.delenv(f"DODEAL_LLM_FALLBACK_{key}", raising=False)
    for key, value in fallback.items():
        monkeypatch.setenv(f"DODEAL_LLM_FALLBACK_{key}", value)
    return _build_settings(_env_file=None)


def _configured(monkeypatch: pytest.MonkeyPatch) -> Settings:
    return _env(
        monkeypatch, PROVIDER="openai", MODEL="fallback-model", API_KEY=FALLBACK_KEY
    )


def _ok(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": f"req-{model}",
            "model": model,
            "choices": [
                {"message": {"content": "{}"}, "finish_reason": "stop", "index": 0}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


class Hosts:
    """Answers per host from a queue, and records every request per host."""

    def __init__(self, primary: list[object], fallback: list[object]) -> None:
        self._queues = {PRIMARY_HOST: list(primary), FALLBACK_HOST: list(fallback)}
        self.requests: dict[str, list[httpx.Request]] = {
            PRIMARY_HOST: [],
            FALLBACK_HOST: [],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests[request.url.host].append(request)
        outcome = self._queues[request.url.host].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, httpx.Response)
        return outcome

    def calls(self, host: str) -> int:
        return len(self.requests[host])


async def _complete(settings: Settings, hosts: Hosts, n: int = 1) -> list[object]:
    results: list[object] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(hosts)) as http:
        client = build_llm_client(settings, http)
        for _ in range(n):
            try:
                results.append(
                    await client.complete(PROMPT, profile=PROFILE_UNIT_A_CLASSIFY)
                )
            except OpenAICompatibleError as exc:
                results.append(exc)
    return results


# --- configuration -------------------------------------------------------------


def test_no_fallback_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset, the factory builds the primary alone."""
    settings = _env(monkeypatch)
    assert settings.llm_fallback_provider is None
    assert settings.llm_fallback_model == ""
    assert settings.llm_fallback_api_key is None
    assert settings.llm_fallback_base_url is None
    assert isinstance(
        build_llm_client(settings, httpx.AsyncClient()), OpenAICompatibleClient
    )


def test_a_configured_fallback_wraps_the_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Set, the factory builds both clients behind one."""
    client = build_llm_client(_configured(monkeypatch), httpx.AsyncClient())
    assert isinstance(client, FallbackLLMClient)
    assert client.primary.breaker is not client.fallback.breaker


@pytest.mark.parametrize(
    ("fallback", "code"),
    [
        ({"PROVIDER": "openai", "MODEL": "m"}, "llm_fallback_api_key_missing:openai"),
        (
            {"PROVIDER": "gemini", "MODEL": "m", "API_KEY": "k"},
            "llm_fallback_provider_not_supported:gemini",
        ),
        ({"PROVIDER": "openai", "API_KEY": "k"}, "llm_fallback_not_configured"),
        ({"MODEL": "m", "API_KEY": "k"}, "llm_fallback_provider_missing"),
        ({"BASE_URL": "https://proxy.test"}, "llm_fallback_provider_missing"),
    ],
    ids=["no-key", "no-adapter", "no-model", "no-provider", "base-url-only"],
)
def test_a_broken_fallback_refuses_to_build(
    monkeypatch: pytest.MonkeyPatch, fallback: dict[str, str], code: str
) -> None:
    """Validated like the primary: a misconfigured fallback is a ConfigError."""
    with pytest.raises(ConfigError) as caught:
        build_llm_client(_env(monkeypatch, **fallback), httpx.AsyncClient())
    assert str(caught.value) == code


# --- when the fallback is used -----------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("refused"),
        httpx.Response(429, json={}),
        httpx.Response(503, json={}),
    ],
    ids=["connect", "429", "503"],
)
async def test_no_response_body_uses_the_fallback_once(
    monkeypatch: pytest.MonkeyPatch, failure: object, caplog: pytest.LogCaptureFixture
) -> None:
    """The fallback answers and its reported model is what comes back."""
    hosts = Hosts([failure], [_ok("fallback-model-reported")])
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.llm.fallback"):
        [response] = await _complete(_configured(monkeypatch), hosts)

    assert response.model == "fallback-model-reported"  # type: ignore[attr-defined]
    assert (hosts.calls(PRIMARY_HOST), hosts.calls(FALLBACK_HOST)) == (1, 1)
    sent = hosts.requests[FALLBACK_HOST][0]
    assert sent.headers["Authorization"] == f"Bearer {FALLBACK_KEY}"
    assert json.loads(sent.content)["model"] == "fallback-model"
    (record,) = [r for r in caplog.records if r.getMessage() == "llm_fallback_used"]
    assert record.primary_reason in {"unavailable", "rate_limited"}


async def test_an_open_primary_breaker_goes_straight_to_the_fallback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Once the primary's breaker is open it is not contacted at all."""
    hosts = Hosts(
        [httpx.ConnectError("refused")],
        [_ok("fallback-model"), _ok("fallback-model")],
    )
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.llm.fallback"):
        await _complete(_configured(monkeypatch), hosts, n=2)

    assert (hosts.calls(PRIMARY_HOST), hosts.calls(FALLBACK_HOST)) == (1, 2)
    reasons = [
        r.primary_reason
        for r in caplog.records
        if r.getMessage() == "llm_fallback_used"
    ]
    assert reasons == ["unavailable", "breaker_open"]


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow connect"),
        httpx.Response(200, text="not json"),
        httpx.Response(500, json={}),
        httpx.Response(502, json={}),
        httpx.Response(400, json={}),
        httpx.Response(401, json={}),
        httpx.ReadError("reset"),
        httpx.PoolTimeout("pool"),
    ],
    ids=[
        "read-timeout",
        "connect-timeout",
        "malformed-200",
        "500",
        "502",
        "400",
        "401",
        "read-error",
        "pool",
    ],
)
async def test_every_other_failure_never_reaches_the_fallback(
    monkeypatch: pytest.MonkeyPatch, failure: object
) -> None:
    """A timeout, a malformed 200 and anything not listed raise the primary's error."""
    hosts = Hosts([failure], [])
    [result] = await _complete(_configured(monkeypatch), hosts)

    assert isinstance(result, OpenAICompatibleError)
    assert result.reason is not LLMErrorReason.BREAKER_OPEN
    assert hosts.calls(FALLBACK_HOST) == 0


async def test_a_failing_fallback_is_not_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once only: the fallback's own failure is the error, with one call each."""
    hosts = Hosts([httpx.Response(503, json={})], [httpx.Response(503, json={})])
    [result] = await _complete(_configured(monkeypatch), hosts)

    assert isinstance(result, OpenAICompatibleError)
    assert result.provider == "openai"
    assert (hosts.calls(PRIMARY_HOST), hosts.calls(FALLBACK_HOST)) == (1, 1)


async def test_a_healthy_primary_never_touches_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback is a second chance, never a second call."""
    hosts = Hosts([_ok("primary-model")], [])
    [response] = await _complete(_configured(monkeypatch), hosts)
    assert response.model == "primary-model"  # type: ignore[attr-defined]
    assert hosts.calls(FALLBACK_HOST) == 0


def test_the_fallback_ignores_the_primarys_profile_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile naming the primary's vendor does not refuse the fallback."""
    monkeypatch.setenv(
        "DODEAL_LLM_PROFILES",
        json.dumps({PROFILE_UNIT_A_CLASSIFY: {"provider": "groq", "model": "g"}}),
    )
    client = build_llm_client(_configured(monkeypatch), httpx.AsyncClient())
    assert isinstance(client, FallbackLLMClient)
