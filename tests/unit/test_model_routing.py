"""Model routing: the provider registry, profiles naming it, and routes.

The guards: with no new config every request body is byte-identical and the
client is the one build_llm_client made; a route sending one pass to a second
provider reaches that provider only."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import ConfigError, Settings, _build_settings, get_settings
from dodeal_ai.core.llm import (
    DEFAULT_ROUTE,
    LLMErrorReason,
    LLMProviderError,
    ModelRouter,
    OpenAICompatibleClient,
    OpenAICompatibleError,
    aclose_llm,
    build_llm_client,
    build_router,
    route_names,
    routed,
    routing,
)
from dodeal_ai.core.llm.profiles import (
    PROFILE_UNIT_A_CLASSIFY,
    PROFILE_UNIT_B_EXTRACT,
    PROFILE_UNIT_B_OBJECTIONS,
    PROFILE_UNIT_B_PROSE,
    task_ceiling,
)
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.paid import PassUsage, _Metered
from dodeal_ai.workers.calls import QUEUES, worker_settings
from tests.helpers.fake_llm import FakeLLM

PROMPT = AssembledPrompt(stable="STABLE", variable="VARIABLE")
DEFAULT_HOST = "api.groq.com"
OWNER_URL = "https://owner.example.test/v1"
OWNER_KEY_ENV = "OWNER_TEST_LLM_KEY"
OWNER = {
    "kind": "openai_compatible",
    "base_url": OWNER_URL,
    "api_key_env": OWNER_KEY_ENV,
    "timeout_seconds": 30,
}


def _answer(model: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "req-1",
            "model": model,
            "choices": [
                {"message": {"content": "{}"}, "finish_reason": "stop", "index": 0}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
    )


class _Host:
    """One provider's transport: every request it saw, answered in turn."""

    def __init__(self, *answers: httpx.Response) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.answers:
            return self.answers.pop(0)
        return _answer("default-answer")

    def bodies(self) -> list[bytes]:
        return [request.content for request in self.requests]


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DODEAL_LLM_PROVIDER", "groq")
    monkeypatch.setenv("DODEAL_LLM_MODEL", "pinned-model")
    monkeypatch.setenv("DODEAL_LLM_API_KEY", "pair-test-key")
    for name, value in env.items():
        monkeypatch.setenv(f"DODEAL_{name}", value)
    return _build_settings(_env_file=None)


@pytest.fixture
def owner_host(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Host]:
    """The registry provider's transport, under every pool the router builds."""
    host = _Host()
    monkeypatch.setenv(OWNER_KEY_ENV, "owner-test-key")
    monkeypatch.setattr(
        routing,
        "new_pool",
        lambda limits: httpx.AsyncClient(transport=httpx.MockTransport(host)),
    )
    yield host


def _pair(settings: Settings, host: _Host) -> OpenAICompatibleClient:
    client = build_llm_client(
        settings, httpx.AsyncClient(transport=httpx.MockTransport(host))
    )
    assert isinstance(client, OpenAICompatibleClient)
    return client


def _routed_env(**routes: dict[str, str]) -> dict[str, str]:
    """A registry with `owner`, its profiles, and the routes given."""
    profiles = {
        "owner.extract": {"provider": "owner", "model": "owner-model"},
        "owner.prose": {"provider": "owner", "model": "owner-model"},
    }
    return {
        "LLM_PROVIDERS": json.dumps({"owner": OWNER}),
        "LLM_PROFILES": json.dumps(profiles),
        "MODEL_ROUTES": json.dumps(routes),
    }


# --- the guards --------------------------------------------------------------


async def test_no_new_config_is_the_same_client_and_the_same_bodies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing routed: build_router hands back the pair's client itself, and
    a Unit A and a Unit B pass send exactly the bodies they always did."""
    host = _Host()
    settings = _settings(monkeypatch)
    pair = _pair(settings, host)

    client = await build_router(settings, pair)
    await client.complete(PROMPT, profile=PROFILE_UNIT_A_CLASSIFY, max_output_tokens=9)
    await client.complete(PROMPT, profile=PROFILE_UNIT_B_EXTRACT, max_output_tokens=7)

    assert client is pair
    assert [json.loads(body) for body in host.bodies()] == [
        {
            "model": "pinned-model",
            "messages": [{"role": "user", "content": PROMPT.text}],
            "temperature": 0.0,
            "max_tokens": ceiling,
            "response_format": {"type": "json_object"},
        }
        for ceiling in (9, 7)
    ]


async def test_a_registry_leaves_the_default_routes_bodies_byte_identical(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    """A registry and a route beside it change nothing a default tenant sends."""
    plain_host, routed_host = _Host(), _Host()
    plain = _pair(_settings(monkeypatch), plain_host)
    for profile in (PROFILE_UNIT_A_CLASSIFY, PROFILE_UNIT_B_EXTRACT):
        await plain.complete(PROMPT, profile=profile, max_output_tokens=5)

    settings = _settings(
        monkeypatch, **_routed_env(device={PROFILE_UNIT_B_EXTRACT: "owner.extract"})
    )
    router = await build_router(settings, _pair(settings, routed_host))
    assert isinstance(router, ModelRouter)
    on_default = routed(router, DEFAULT_ROUTE)
    for profile in (PROFILE_UNIT_A_CLASSIFY, PROFILE_UNIT_B_EXTRACT):
        await on_default.complete(PROMPT, profile=profile, max_output_tokens=5)

    assert routed_host.bodies() == plain_host.bodies()
    assert owner_host.requests == []
    await aclose_llm(router)


async def test_a_route_sends_one_pass_to_a_second_provider_only(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    """On `device`, extract reaches the owner's server and nothing else does."""
    pair_host = _Host()
    settings = _settings(
        monkeypatch, **_routed_env(device={PROFILE_UNIT_B_EXTRACT: "owner.extract"})
    )
    router = await build_router(settings, _pair(settings, pair_host))
    device = routed(router, "device")

    answered = await device.complete(
        PROMPT, profile=PROFILE_UNIT_B_EXTRACT, max_output_tokens=5
    )
    await device.complete(PROMPT, profile=PROFILE_UNIT_B_PROSE, max_output_tokens=5)

    (sent,) = owner_host.requests
    assert sent.url.host == "owner.example.test"
    assert sent.headers["authorization"] == "Bearer owner-test-key"
    assert json.loads(sent.content)["model"] == "owner-model"
    assert answered.provider == "owner"
    (prose,) = pair_host.requests
    assert prose.url.host == DEFAULT_HOST
    assert json.loads(prose.content)["model"] == "pinned-model"
    await aclose_llm(router)


# --- the registry ------------------------------------------------------------


async def test_a_pass_profile_naming_the_registry_is_routed_on_default(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    """A pass's own-name profile may name a registry provider directly."""
    pair_host = _Host()
    profiles = {PROFILE_UNIT_B_EXTRACT: {"provider": "owner", "model": "own"}}
    settings = _settings(
        monkeypatch,
        LLM_PROVIDERS=json.dumps({"owner": OWNER}),
        LLM_PROFILES=json.dumps(profiles),
    )
    router = await build_router(settings, _pair(settings, pair_host))
    await router.complete(PROMPT, profile=PROFILE_UNIT_B_EXTRACT, max_output_tokens=5)
    await router.complete(PROMPT, profile=PROFILE_UNIT_A_CLASSIFY, max_output_tokens=5)
    assert len(owner_host.requests) == 1 and len(pair_host.requests) == 1
    assert set(router.providers) == {"owner"}
    await aclose_llm(router)


async def test_an_empty_key_variable_refuses_naming_the_provider_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(OWNER_KEY_ENV, raising=False)
    settings = _settings(monkeypatch, **_routed_env())
    with pytest.raises(ConfigError, match="^llm_provider_key_missing:owner$"):
        await build_router(settings, FakeLLM())


def test_a_key_is_never_taken_from_the_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spec has no field for a key: one in the JSON refuses to load."""
    spec = {**OWNER, "api_key": "sk-in-the-json"}
    with pytest.raises(ConfigError) as caught:
        _settings(monkeypatch, LLM_PROVIDERS=json.dumps({"owner": spec}))
    assert "sk-in-the-json" not in str(caught.value)


@pytest.mark.parametrize(
    "env",
    [
        # A profile naming a provider nobody registered.
        {"LLM_PROFILES": '{"unit_a.vague":{"provider":"nobody","model":"m"}}'},
        # A registry name a profile could not tell from the pair's.
        {"LLM_PROVIDERS": json.dumps({"groq": OWNER})},
        {"LLM_PROVIDERS": json.dumps({"default": OWNER})},
        # A registry timeout the watchdog would always beat.
        {"LLM_PROVIDERS": json.dumps({"owner": {**OWNER, "timeout_seconds": 61}})},
        # A key, an http-less URL or a kind this service has no adapter for.
        {"LLM_PROVIDERS": json.dumps({"owner": {**OWNER, "kind": "anthropic"}})},
        {"LLM_PROVIDERS": json.dumps({"owner": {**OWNER, "base_url": "owner:1"}})},
        # A fallback that names itself or nothing.
        {
            "LLM_PROFILES": '{"a":{"provider":"groq","model":"m","fallback_profile":"a"}}'
        },
        {
            "LLM_PROFILES": '{"a":{"provider":"groq","model":"m","fallback_profile":"b"}}'
        },
        # A route that is "default", or names a profile nobody configured.
        {"MODEL_ROUTES": '{"default":{}}'},
        {"MODEL_ROUTES": '{"device":{"unit_b.extract":"nobody"}}'},
    ],
)
def test_a_name_that_does_not_resolve_refuses_at_load(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    with pytest.raises(ConfigError):
        _settings(monkeypatch, **env)


async def test_a_route_naming_a_pass_that_does_not_exist_refuses_startup(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    settings = _settings(monkeypatch, **_routed_env(device={"unit_c.x": "owner.prose"}))
    with pytest.raises(ConfigError, match="^model_route_unknown_pass:device$"):
        await build_router(settings, FakeLLM())


async def test_a_bad_registry_profile_refuses_and_closes_every_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The registry's half of the sweep: a temperature out of this API's range."""
    pools: list[httpx.AsyncClient] = []

    def _pool(limits: httpx.Limits | None) -> httpx.AsyncClient:
        pools.append(httpx.AsyncClient())
        return pools[-1]

    monkeypatch.setattr(routing, "new_pool", _pool)
    monkeypatch.setenv(OWNER_KEY_ENV, "owner-test-key")
    profiles = {"owner.x": {"provider": "owner", "model": "m", "temperature": 1.0}}
    settings = _settings(
        monkeypatch,
        LLM_PROVIDERS=json.dumps({"owner": OWNER}),
        LLM_PROFILES=json.dumps(profiles),
    )
    bad = settings.llm_profiles["owner.x"].model_copy(update={"temperature": 3.0})
    settings = settings.model_copy(update={"llm_profiles": {"owner.x": bad}})
    with pytest.raises(ConfigError, match="^llm_temperature_out_of_range:owner$"):
        await build_router(settings, FakeLLM())
    assert pools and all(pool.is_closed for pool in pools)


async def test_an_http_registry_url_is_an_error_line_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The owner's server may have no certificate: loud, never silent."""
    monkeypatch.setenv(OWNER_KEY_ENV, "owner-test-key")
    spec = {**OWNER, "base_url": "http://10.0.0.5:8000/v1"}
    settings = _settings(monkeypatch, LLM_PROVIDERS=json.dumps({"owner": spec}))
    with caplog.at_level(logging.ERROR, logger="dodeal_ai.startup"):
        router = await build_router(settings, FakeLLM(), limits=httpx.Limits())
    (line,) = [r for r in caplog.records if r.getMessage() == "llm_provider_insecure"]
    assert line.provider == "owner" and "10.0.0.5" not in caplog.text
    await aclose_llm(router)


async def test_the_router_closes_the_registry_pools_and_leaves_the_pairs(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    pair_pool = httpx.AsyncClient()
    settings = _settings(monkeypatch, **_routed_env())
    router = await build_router(settings, build_llm_client(settings, pair_pool))
    assert isinstance(router, ModelRouter)
    pool = router.providers["owner"]._http
    await aclose_llm(router)
    await aclose_llm(None)
    assert pool.is_closed and not pair_pool.is_closed
    await pair_pool.aclose()


# --- routes a tenant names ---------------------------------------------------


def test_the_route_names_a_tenant_may_choose(monkeypatch: pytest.MonkeyPatch) -> None:
    assert route_names(_settings(monkeypatch)) == {"default"}
    settings = _settings(monkeypatch, **_routed_env(device={}))
    assert route_names(settings) == {"default", "device"}


async def test_an_unknown_route_fails_closed_and_sends_nothing(
    monkeypatch: pytest.MonkeyPatch,
    owner_host: _Host,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A route this process does not know never falls back onto another."""
    pair_host = _Host()
    settings = _settings(monkeypatch, **_routed_env())
    router = await build_router(settings, _pair(settings, pair_host))
    fake = FakeLLM()
    for client in (routed(router, "gone"), routed(fake, "gone")):
        with pytest.raises(LLMProviderError) as caught:
            await client.complete(PROMPT, profile=PROFILE_UNIT_B_EXTRACT)
        assert caught.value.reason is LLMErrorReason.ROUTE_NOT_CONFIGURED
        assert not caught.value.transient
    assert routed(fake, DEFAULT_ROUTE) is fake
    assert pair_host.requests == owner_host.requests == [] and fake.call_count == 0
    assert "model_route_not_configured" in caplog.text
    await aclose_llm(router)


# --- fallback_profile, one hop ----------------------------------------------


def _fallback_env() -> dict[str, str]:
    profiles = {
        "owner.extract": {
            "provider": "owner",
            "model": "owner-model",
            "fallback_profile": "api.extract",
        },
        "api.extract": {"provider": "groq", "model": "api-model"},
    }
    return {
        "LLM_PROVIDERS": json.dumps({"owner": OWNER}),
        "LLM_PROFILES": json.dumps(profiles),
        "MODEL_ROUTES": json.dumps({"device": {"unit_b.extract": "owner.extract"}}),
    }


@pytest.mark.parametrize("status", [503, 429])
async def test_no_response_body_tries_the_fallback_profile_once(
    monkeypatch: pytest.MonkeyPatch,
    owner_host: _Host,
    caplog: pytest.LogCaptureFixture,
    status: int,
) -> None:
    owner_host.answers = [httpx.Response(status)]
    pair_host = _Host(_answer("api-model"))
    settings = _settings(monkeypatch, **_fallback_env())
    router = await build_router(settings, _pair(settings, pair_host))

    answered = await routed(router, "device").complete(
        PROMPT, profile=PROFILE_UNIT_B_EXTRACT, max_output_tokens=5
    )

    assert len(owner_host.requests) == 1 and len(pair_host.requests) == 1
    assert json.loads(pair_host.requests[0].content)["model"] == "api-model"
    assert (answered.provider, answered.model) == ("groq", "api-model")
    (line,) = [r for r in caplog.records if r.getMessage() == "llm_fallback_used"]
    assert (line.route, line.profile) == ("device", "owner.extract")
    await aclose_llm(router)


@pytest.mark.parametrize("status", [500, 400])
async def test_a_response_that_arrived_is_never_sent_to_the_fallback(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host, status: int
) -> None:
    owner_host.answers = [httpx.Response(status)]
    pair_host = _Host()
    settings = _settings(monkeypatch, **_fallback_env())
    router = await build_router(settings, _pair(settings, pair_host))
    with pytest.raises(OpenAICompatibleError):
        await routed(router, "device").complete(
            PROMPT, profile=PROFILE_UNIT_B_EXTRACT, max_output_tokens=5
        )
    assert len(owner_host.requests) == 1 and pair_host.requests == []
    await aclose_llm(router)


async def test_a_profile_without_a_fallback_re_raises(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    owner_host.answers = [httpx.Response(503)]
    settings = _settings(
        monkeypatch, **_routed_env(device={PROFILE_UNIT_B_EXTRACT: "owner.extract"})
    )
    router = await build_router(settings, FakeLLM())
    with pytest.raises(OpenAICompatibleError):
        await routed(router, "device").complete(
            PROMPT, profile=PROFILE_UNIT_B_EXTRACT, max_output_tokens=5
        )
    await aclose_llm(router)


# --- ceilings and stamps -----------------------------------------------------


async def test_a_ceiling_follows_the_profile_the_route_sends_to(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    """A pass routed to a reasoning profile gets its reasoning ceiling."""
    profiles = {
        "owner.think": {"provider": "owner", "model": "m", "reasoning_effort": "low"}
    }
    routes = {"device": {PROFILE_UNIT_B_OBJECTIONS: "owner.think"}}
    settings = _settings(
        monkeypatch,
        LLM_PROVIDERS=json.dumps({"owner": OWNER}),
        LLM_PROFILES=json.dumps(profiles),
        MODEL_ROUTES=json.dumps(routes),
    )
    router = await build_router(settings, FakeLLM())
    assert isinstance(router, ModelRouter)

    def ceiling(client: object) -> int:
        return task_ceiling(
            settings, PROFILE_UNIT_B_OBJECTIONS, plain=1, reasoning=2, client=client
        )

    assert ceiling(routed(router, "device")) == 2
    # A pass's metered client hands the question to the routed client inside.
    metered = _Metered(routed(router, "device"), PassUsage(), "objections", {})
    assert ceiling(metered) == 2
    assert ceiling(router) == ceiling(FakeLLM()) == ceiling(None) == 1
    assert router.profile_for(PROFILE_UNIT_B_OBJECTIONS) is None
    await aclose_llm(router)


async def test_every_response_names_the_provider_that_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Host()
    client = _pair(_settings(monkeypatch), host)
    answered = await client.complete(PROMPT, profile=PROFILE_UNIT_A_CLASSIFY)
    assert answered.provider == "groq" == client.provider


# --- built once, at startup, in the API and every worker -----------------------


def _startup_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _settings(monkeypatch, **_routed_env(device={}))
    get_settings.cache_clear()


def test_the_api_builds_one_router_and_closes_its_pools(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    _startup_env(monkeypatch)
    with TestClient(app):
        router = app.state.llm
        assert isinstance(router, ModelRouter)
        pool = router.providers["owner"]._http
        assert not pool.is_closed
    assert pool.is_closed
    get_settings.cache_clear()


async def test_every_worker_builds_the_router_and_closes_its_pools(
    monkeypatch: pytest.MonkeyPatch, owner_host: _Host
) -> None:
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    _startup_env(monkeypatch)
    for queue in QUEUES.values():
        built = worker_settings(queue, transcriber=FakeTranscriber())
        ctx: dict[str, Any] = {}
        await built["on_startup"](ctx)
        assert isinstance(ctx["llm"], ModelRouter)
        pool = ctx["llm"].providers["owner"]._http
        await built["on_shutdown"](ctx)
        assert pool.is_closed, queue
    get_settings.cache_clear()
