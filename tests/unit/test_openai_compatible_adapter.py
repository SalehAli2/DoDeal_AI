"""Conformance for the OpenAI-compatible adapter, on httpx.MockTransport.

Item 76 part 1. Hermetic: no network, no key, no provider SDK. Every test below
runs against BOTH base URLs, because "Groq and OpenAI are the same API" is the
claim the single class rests on -- if one of them ever needs a different branch,
these are the tests that have to change first and say so.

The API key here is the literal `test-key` and the failure bodies carry
BODY_SENTINEL. Both exist to be searched for: the key must appear in the bearer
header and in nothing else, and the sentinel must appear nowhere at all.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from pydantic import SecretStr

from dodeal_ai.core.config import (
    ConfigError,
    LLMProvider,
    Settings,
    _build_settings,
)
from dodeal_ai.core.llm import build_llm_client
from dodeal_ai.core.llm.client import (
    FinishReason,
    LLMClient,
    LLMConfigurationError,
    LLMErrorReason,
    LLMProviderError,
)
from dodeal_ai.core.llm.openai_compatible import (
    BASE_URLS,
    GROQ_BASE_URL,
    OPENAI_BASE_URL,
    TEMPERATURE_MAX,
    UNKNOWN_PROVIDER_NAME,
    OpenAICompatibleClient,
    OpenAICompatibleError,
    provider_name_for,
)
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_CLASSIFY, ResolvedProfile
from dodeal_ai.core.logging_config import configure_logging
from dodeal_ai.core.prompting import AssembledPrompt

API_KEY = "test-key"
BODY_SENTINEL = "sentinel-provider-detail-4f2a"
PINNED_MODEL = "pinned-model-2026-01-01"
REPORTED_MODEL = "pinned-model-2026-01-01-0125"

# Every test is parametrised over both, so a divergence between the two vendors
# fails here rather than in production on whichever one was not tested.
BOTH_URLS = pytest.mark.parametrize(
    "base_url", [GROQ_BASE_URL, OPENAI_BASE_URL], ids=["groq", "openai"]
)

PROMPT = AssembledPrompt(
    stable="STABLE-SYSTEM-TEMPLATE",
    variable="<<<DATA>>>VARIABLE-CALLER-DATA<<<END>>>",
    tail="TAIL-STRICTER-INSTRUCTION",
)


# --- scaffolding ------------------------------------------------------------


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Settings from env with no .env file in play, so a stray local file cannot
    supply a profile or a key and mask the path under test."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-only-key")
    for key in ("PROVIDER", "MODEL", "PROFILES", "API_KEY", "BASE_URL"):
        monkeypatch.delenv(f"DODEAL_LLM_{key}", raising=False)
    env.setdefault("PROVIDER", LLMProvider.GROQ.value)
    env.setdefault("MODEL", PINNED_MODEL)
    for key, value in env.items():
        monkeypatch.setenv(f"DODEAL_LLM_{key}", value)
    return _build_settings(_env_file=None)


def _ok_body(
    *,
    content: str = '{"note_type": "callback"}',
    finish_reason: str = "stop",
    **extra: object,
) -> dict[str, object]:
    body: dict[str, object] = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1757000000,
        "model": REPORTED_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 411, "completion_tokens": 29, "total_tokens": 440},
    }
    body.update(extra)
    return body


class Recorder:
    """A MockTransport handler that records every request it was given.

    `calls` is the assertion that matters on a failure path: the adapter must
    never retry a paid call, so the count is checked at exactly one on every
    error case below, not only on the happy one.
    """

    def __init__(
        self,
        response: httpx.Response | None = None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self.requests: list[httpx.Request] = []
        self._response = response
        self._raises = raises

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    @property
    def calls(self) -> int:
        return len(self.requests)

    @property
    def body(self) -> dict[str, object]:
        return json.loads(self.requests[-1].content)


def _failure(status: int) -> httpx.Response:
    """A provider error whose body carries the sentinel, the way a real one
    carries a message naming the model, the org and sometimes the prompt."""
    return httpx.Response(
        status,
        json={"error": {"message": BODY_SENTINEL, "type": "invalid_request_error"}},
    )


def _client(
    settings: Settings, base_url: str, http: httpx.AsyncClient
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url, PINNED_MODEL, SecretStr(API_KEY), http, settings=settings
    )


async def _call(
    settings: Settings,
    base_url: str,
    recorder: Recorder,
    *,
    profile: str = PROFILE_UNIT_A_CLASSIFY,
    max_output_tokens: int | None = None,
) -> object:
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        return await _client(settings, base_url, http).complete(
            PROMPT, profile=profile, max_output_tokens=max_output_tokens
        )


# --- the request body -------------------------------------------------------


@BOTH_URLS
async def test_request_demands_json_mode(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """JSON mode is on every request, unconditionally."""
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder)
    assert recorder.body["response_format"] == {"type": "json_object"}


@BOTH_URLS
async def test_request_posts_to_chat_completions_under_the_base_url(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """The path is the vendor's, the host is the base URL's."""
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder)
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"{base_url}/chat/completions"


@BOTH_URLS
async def test_request_carries_the_profile_temperature_and_model(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """A configured profile decides the temperature and the model, not the pin."""
    profiles = json.dumps(
        {
            PROFILE_UNIT_A_CLASSIFY: {
                "provider": "groq",
                "model": "profile-chosen-model",
                "temperature": 0.7,
            }
        }
    )
    settings = _settings(monkeypatch, PROFILES=profiles)
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(settings, base_url, recorder)
    assert recorder.body["temperature"] == 0.7
    assert recorder.body["model"] == "profile-chosen-model"


@BOTH_URLS
async def test_unprofiled_name_falls_back_to_the_pinned_pair_at_zero(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """The fallback rule reaches the wire: the pinned model at temperature 0."""
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder)
    assert recorder.body["model"] == PINNED_MODEL
    assert recorder.body["temperature"] == 0.0


@BOTH_URLS
async def test_request_carries_the_task_ceiling(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """max_tokens is the caller's number when no profile lowers it."""
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder, max_output_tokens=512)
    assert recorder.body["max_tokens"] == 512


@BOTH_URLS
async def test_request_falls_back_to_the_settings_ceiling(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """No caller ceiling means Settings.llm_max_output_tokens."""
    settings = _settings(monkeypatch, MAX_OUTPUT_TOKENS="777")
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(settings, base_url, recorder)
    assert recorder.body["max_tokens"] == 777


@BOTH_URLS
@pytest.mark.parametrize(
    ("profile_ceiling", "task_ceiling", "expected"),
    [(256, 512, 256), (2048, 512, 512)],
    ids=["profile-lowers", "profile-cannot-raise"],
)
async def test_a_profile_may_lower_the_ceiling_never_raise_it(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    profile_ceiling: int,
    task_ceiling: int,
    expected: int,
) -> None:
    """The ceiling rule, observed on the wire rather than on the dataclass."""
    profiles = json.dumps(
        {
            PROFILE_UNIT_A_CLASSIFY: {
                "provider": "openai",
                "model": PINNED_MODEL,
                "max_output_tokens": profile_ceiling,
            }
        }
    )
    settings = _settings(monkeypatch, PROFILES=profiles)
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(settings, base_url, recorder, max_output_tokens=task_ceiling)
    assert recorder.body["max_tokens"] == expected


@BOTH_URLS
async def test_prompt_is_one_user_message_in_stable_variable_tail_order(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """One message, carrying prompt.text unchanged and unsplit."""
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder)
    messages = recorder.body["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    assert messages[0]["role"] == "user"
    content = messages[0]["content"]
    assert content == PROMPT.text
    assert (
        content.index(PROMPT.stable)
        < content.index(PROMPT.variable)
        < content.index(PROMPT.tail)
    )


# --- the key ----------------------------------------------------------------


@BOTH_URLS
async def test_key_is_sent_as_a_bearer_header(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder)
    assert recorder.requests[0].headers["authorization"] == f"Bearer {API_KEY}"


@BOTH_URLS
async def test_key_is_never_in_the_body_or_the_url(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    await _call(_settings(monkeypatch), base_url, recorder)
    assert API_KEY not in recorder.requests[0].content.decode()
    assert API_KEY not in str(recorder.requests[0].url)


@BOTH_URLS
async def test_key_reaches_no_log_line_on_success_or_failure(
    monkeypatch: pytest.MonkeyPatch, base_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    """The sentinel search, over the formatted text AND every record's fields."""
    settings = _settings(monkeypatch)
    with caplog.at_level(logging.DEBUG):
        await _call(settings, base_url, Recorder(httpx.Response(200, json=_ok_body())))
        with pytest.raises(LLMProviderError):
            await _call(settings, base_url, Recorder(_failure(500)))
    assert API_KEY not in caplog.text
    assert not [r for r in caplog.records if API_KEY in repr(r.__dict__)]


# --- the response -----------------------------------------------------------


@BOTH_URLS
async def test_response_maps_onto_the_seam(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """Text, both token counts, the REPORTED model, and the request id."""
    recorder = Recorder(httpx.Response(200, json=_ok_body(content='{"a": 1}')))
    result = await _call(_settings(monkeypatch), base_url, recorder)
    assert result.text == '{"a": 1}'
    assert result.input_tokens == 411
    assert result.output_tokens == 29
    assert result.total_tokens == 440
    assert result.model == REPORTED_MODEL
    assert result.provider_request_id == "chatcmpl-abc123"


@BOTH_URLS
@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        ("stop", FinishReason.STOP),
        ("length", FinishReason.MAX_TOKENS),
        ("content_filter", FinishReason.OTHER),
        ("tool_calls", FinishReason.OTHER),
    ],
)
async def test_finish_reason_mapping(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    reported: str,
    expected: FinishReason,
) -> None:
    """stop and length are named; anything else is OTHER and never raises."""
    recorder = Recorder(httpx.Response(200, json=_ok_body(finish_reason=reported)))
    result = await _call(_settings(monkeypatch), base_url, recorder)
    assert result.finish_reason is expected


@BOTH_URLS
async def test_missing_id_is_none_not_a_failure(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """provider_request_id is a reconciliation handle, not part of the answer."""
    body = _ok_body()
    del body["id"]
    result = await _call(
        _settings(monkeypatch), base_url, Recorder(httpx.Response(200, json=body))
    )
    assert result.provider_request_id is None


# --- failures ---------------------------------------------------------------


_STATUS_CASES = [
    (400, LLMErrorReason.INVALID_REQUEST, False),
    (401, LLMErrorReason.AUTH, False),
    (403, LLMErrorReason.AUTH, False),
    (429, LLMErrorReason.RATE_LIMITED, True),
    (500, LLMErrorReason.UNAVAILABLE, True),
    (503, LLMErrorReason.UNAVAILABLE, True),
]


@BOTH_URLS
@pytest.mark.parametrize(("status", "reason", "transient"), _STATUS_CASES)
async def test_status_maps_to_reason_and_transience(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    status: int,
    reason: LLMErrorReason,
    transient: bool,
) -> None:
    """The six statuses the brief names, each with its status kept."""
    recorder = Recorder(_failure(status))
    with pytest.raises(OpenAICompatibleError) as caught:
        await _call(_settings(monkeypatch), base_url, recorder)
    assert caught.value.reason is reason
    assert caught.value.transient is transient
    assert caught.value.status == status
    assert caught.value.provider == provider_name_for(base_url)


@BOTH_URLS
@pytest.mark.parametrize(("status", "reason", "transient"), _STATUS_CASES)
async def test_failure_never_carries_the_provider_body(
    monkeypatch: pytest.MonkeyPatch,
    base_url: str,
    status: int,
    reason: LLMErrorReason,
    transient: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """str, repr and every log line stay free of the provider's message."""
    with (
        caplog.at_level(logging.DEBUG),
        pytest.raises(OpenAICompatibleError) as caught,
    ):
        await _call(_settings(monkeypatch), base_url, Recorder(_failure(status)))
    err = caught.value
    assert str(err) == f"llm_provider_error:{reason.value}"
    assert BODY_SENTINEL not in str(err)
    assert BODY_SENTINEL not in repr(err)
    assert BODY_SENTINEL not in caplog.text
    assert not [r for r in caplog.records if BODY_SENTINEL in repr(r.__dict__)]


@BOTH_URLS
@pytest.mark.parametrize("status", [s for s, _, _ in _STATUS_CASES])
async def test_transport_is_entered_exactly_once_on_a_failed_status(
    monkeypatch: pytest.MonkeyPatch, base_url: str, status: int
) -> None:
    """No retry of a paid call, on any status."""
    recorder = Recorder(_failure(status))
    with pytest.raises(LLMProviderError):
        await _call(_settings(monkeypatch), base_url, recorder)
    assert recorder.calls == 1


@BOTH_URLS
async def test_an_unlisted_4xx_is_unknown_and_not_transient(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """A 404 on a mistyped base URL must not be retried into a second bill."""
    with pytest.raises(OpenAICompatibleError) as caught:
        await _call(_settings(monkeypatch), base_url, Recorder(_failure(404)))
    assert caught.value.reason is LLMErrorReason.UNKNOWN
    assert caught.value.transient is False
    assert caught.value.status == 404


@BOTH_URLS
async def test_timeout_is_the_transient_seam_error_never_a_builtin(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """Catch-list 55: never a builtin TimeoutError out of this adapter."""
    recorder = Recorder(raises=httpx.ReadTimeout("read timed out"))
    with pytest.raises(OpenAICompatibleError) as caught:
        await _call(_settings(monkeypatch), base_url, recorder)
    assert not isinstance(caught.value, TimeoutError)
    assert caught.value.reason is LLMErrorReason.UNAVAILABLE
    assert caught.value.transient is True
    assert caught.value.status is None
    assert recorder.calls == 1


@BOTH_URLS
async def test_a_transport_error_is_transient(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    recorder = Recorder(raises=httpx.ConnectError("no route to host"))
    with pytest.raises(OpenAICompatibleError) as caught:
        await _call(_settings(monkeypatch), base_url, recorder)
    assert caught.value.transient is True
    assert recorder.calls == 1


@BOTH_URLS
async def test_a_non_json_200_is_transient(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """A gateway's HTML error page arriving with a 200."""
    recorder = Recorder(
        httpx.Response(200, text=f"<html>gateway {BODY_SENTINEL}</html>")
    )
    with pytest.raises(OpenAICompatibleError) as caught:
        await _call(_settings(monkeypatch), base_url, recorder)
    assert caught.value.transient is True
    assert caught.value.reason is LLMErrorReason.UNKNOWN
    assert BODY_SENTINEL not in str(caught.value)
    assert recorder.calls == 1


@BOTH_URLS
@pytest.mark.parametrize(
    "mangle",
    [
        lambda b: b.pop("choices"),
        lambda b: b.pop("usage"),
        lambda b: b.pop("model"),
        lambda b: b["choices"].clear(),
        lambda b: b["choices"][0].pop("finish_reason"),
        lambda b: b["choices"][0]["message"].pop("content"),
        lambda b: b["usage"].pop("prompt_tokens"),
        lambda b: b["choices"][0]["message"].update({"content": None}),
    ],
    ids=[
        "no-choices",
        "no-usage",
        "no-model",
        "empty-choices",
        "no-finish-reason",
        "no-content",
        "no-prompt-tokens",
        "null-content",
    ],
)
async def test_a_missing_field_is_transient(
    monkeypatch: pytest.MonkeyPatch, base_url: str, mangle: object
) -> None:
    """Every field the mapping reads, absent one at a time."""
    body = _ok_body()
    mangle(body)  # type: ignore[operator]
    recorder = Recorder(httpx.Response(200, json=body))
    with pytest.raises(OpenAICompatibleError) as caught:
        await _call(_settings(monkeypatch), base_url, recorder)
    assert caught.value.transient is True
    assert recorder.calls == 1


# --- resolution refuses before anything is spent ----------------------------


@BOTH_URLS
async def test_a_temperature_over_the_bound_fails_at_resolution(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """Over the bound is a ConfigError, and the transport is never entered.

    The out-of-range profile is injected at resolve_profile rather than through
    DODEAL_LLM_PROFILES because ModelProfile's own field caps at 1.0 -- so this
    bound is the adapter's second line, and a widened field would hit it here.
    """
    settings = _settings(monkeypatch)
    monkeypatch.setattr(
        "dodeal_ai.core.llm.openai_compatible.resolve_profile",
        lambda _s, _n: ResolvedProfile(
            provider=LLMProvider.GROQ,
            model=PINNED_MODEL,
            temperature=TEMPERATURE_MAX + 0.5,
            max_output_tokens=None,
        ),
    )
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    with pytest.raises(ConfigError) as caught:
        await _call(settings, base_url, recorder)
    assert "llm_temperature_out_of_range" in str(caught.value)
    assert recorder.calls == 0


@BOTH_URLS
async def test_a_profile_naming_another_vendor_fails_at_resolution(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    """An anthropic profile must not post an anthropic model id to this URL."""
    profiles = json.dumps(
        {
            PROFILE_UNIT_A_CLASSIFY: {
                "provider": "anthropic",
                "model": "claude-something",
            }
        }
    )
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    with pytest.raises(ConfigError) as caught:
        await _call(_settings(monkeypatch, PROFILES=profiles), base_url, recorder)
    assert "llm_profile_provider_mismatch" in str(caught.value)
    assert recorder.calls == 0


@BOTH_URLS
async def test_resolution_errors_never_quote_the_key(
    monkeypatch: pytest.MonkeyPatch, base_url: str
) -> None:
    settings = _settings(monkeypatch)
    monkeypatch.setattr(
        "dodeal_ai.core.llm.openai_compatible.resolve_profile",
        lambda _s, _n: ResolvedProfile(
            provider=LLMProvider.GROQ,
            model=PINNED_MODEL,
            temperature=-1.0,
            max_output_tokens=None,
        ),
    )
    with pytest.raises(ConfigError) as caught:
        await _call(settings, base_url, Recorder(httpx.Response(200)))
    assert API_KEY not in str(caught.value)


# --- the factory ------------------------------------------------------------


@pytest.mark.parametrize("provider", ["groq", "openai"])
async def test_build_llm_client_returns_the_adapter(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Both supported providers build, and land on the vendor's own base URL."""
    settings = _settings(monkeypatch, PROVIDER=provider, API_KEY=API_KEY)
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        client: LLMClient = build_llm_client(settings, http)
        assert isinstance(client, OpenAICompatibleClient)
        assert isinstance(client, LLMClient)
        await client.complete(PROMPT, profile=PROFILE_UNIT_A_CLASSIFY)
    expected = BASE_URLS[LLMProvider(provider)]
    assert str(recorder.requests[0].url) == f"{expected}/chat/completions"


@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
async def test_build_llm_client_refuses_a_provider_with_no_adapter(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    """Named, so a deployment is told which half is missing."""
    settings = _settings(monkeypatch, PROVIDER=provider, API_KEY=API_KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Recorder())) as http:
        with pytest.raises(ConfigError) as caught:
            build_llm_client(settings, http)
    assert str(caught.value) == f"llm_provider_not_supported:{provider}"


@pytest.mark.parametrize("provider", ["groq", "openai"])
async def test_build_llm_client_refuses_a_missing_key_without_quoting_one(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    settings = _settings(monkeypatch, PROVIDER=provider)
    assert settings.llm_api_key is None
    async with httpx.AsyncClient(transport=httpx.MockTransport(Recorder())) as http:
        with pytest.raises(ConfigError) as caught:
            build_llm_client(settings, http)
    assert str(caught.value) == f"llm_api_key_missing:{provider}"
    assert API_KEY not in str(caught.value)


async def test_build_llm_client_refuses_an_unconfigured_seam(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pinned model is the seam's own fixed failure, not a new one."""
    settings = _settings(monkeypatch, MODEL="", API_KEY=API_KEY)
    async with httpx.AsyncClient(transport=httpx.MockTransport(Recorder())) as http:
        with pytest.raises(LLMConfigurationError) as caught:
            build_llm_client(settings, http)
    assert str(caught.value) == "llm_not_configured"


async def test_base_url_override_wins_and_is_never_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy override is used verbatim and labelled generically, because its
    host or path can itself be the credential."""
    proxy = "https://llm-proxy.internal/v1"
    settings = _settings(monkeypatch, API_KEY=API_KEY, BASE_URL=proxy)
    recorder = Recorder(httpx.Response(200, json=_ok_body()))
    async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
        await build_llm_client(settings, http).complete(
            PROMPT, profile=PROFILE_UNIT_A_CLASSIFY
        )
    assert str(recorder.requests[0].url) == f"{proxy}/chat/completions"
    assert provider_name_for(proxy) == UNKNOWN_PROVIDER_NAME


async def test_a_failure_behind_a_proxy_names_no_host(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Nothing WE log carries the proxy path, which can itself be a credential.

    Scoped to `dodeal_ai` records on purpose. httpx logs every request line,
    URL included, on its own `httpx` logger at INFO -- and caplog.at_level here
    forces the root level down far enough to capture it. In the service that
    line is never emitted: configure_logging() leaves the ROOT logger at WARNING
    and raises only `dodeal_ai` to settings.log_level, so a third-party INFO
    line is dropped before any handler even at DODEAL_LOG_LEVEL=DEBUG.
    """
    proxy = "https://key-in-the-path.internal/abcd1234/v1"
    settings = _settings(monkeypatch, API_KEY=API_KEY, BASE_URL=proxy)
    recorder = Recorder(_failure(500))
    with caplog.at_level(logging.DEBUG):
        async with httpx.AsyncClient(transport=httpx.MockTransport(recorder)) as http:
            with pytest.raises(OpenAICompatibleError) as caught:
                await build_llm_client(settings, http).complete(
                    PROMPT, profile=PROFILE_UNIT_A_CLASSIFY
                )
    assert caught.value.provider == UNKNOWN_PROVIDER_NAME
    ours = [r for r in caplog.records if r.name.startswith("dodeal_ai")]
    assert ours, "the adapter logged nothing, so this guard proved nothing"
    assert not [r for r in ours if "abcd1234" in repr(r.__dict__)]


def test_third_party_request_lines_stay_below_the_root_floor() -> None:
    """The other half of the test above, asserted rather than asserted-about.

    httpx's request line carries the full URL. It reaches no handler because the
    root logger sits at WARNING and only `dodeal_ai` is raised -- so this is the
    line that keeps a proxy credential out of the logs, and it is here so that
    lowering the root floor fails a test instead of leaking quietly.
    """
    configure_logging()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# --- the seam's own contract ------------------------------------------------


def test_the_adapter_satisfies_the_protocol_at_runtime() -> None:
    assert issubclass(OpenAICompatibleClient, LLMClient)


def test_the_error_is_a_seam_error_so_callers_branch_on_transient() -> None:
    """Nothing downstream knows this subclass exists; it must arrive as the
    seam's own type at every caller that catches one."""
    err = OpenAICompatibleError(
        LLMErrorReason.AUTH, transient=False, status=401, provider="groq"
    )
    assert isinstance(err, LLMProviderError)
    assert str(err) == "llm_provider_error:auth"


def test_both_supported_providers_have_a_base_url() -> None:
    """Fail closed on a provider added to the enum without a URL beside it."""
    assert set(BASE_URLS) == {LLMProvider.GROQ, LLMProvider.OPENAI}
    assert all(url.startswith("https://") for url in BASE_URLS.values())
