"""Step 1 — the LLM seam. Hermetic: no network, no provider SDK."""

from __future__ import annotations

import dataclasses
import inspect
import typing

import pytest

from dodeal_ai.core import llm as llm_pkg
from dodeal_ai.core.config import ConfigError, LLMProvider, Settings, _build_settings
from dodeal_ai.core.llm import (
    FinishReason,
    LLMClient,
    LLMConfigurationError,
    LLMErrorReason,
    LLMProviderError,
    LLMResponse,
    get_llm_client,
)
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt


def _response(**overrides: object) -> LLMResponse:
    base: dict[str, object] = {
        "text": '{"note_type": "callback"}',
        "input_tokens": 120,
        "output_tokens": 30,
        "model": "test-model-pinned",
        "finish_reason": FinishReason.STOP,
    }
    base.update(overrides)
    return LLMResponse(**base)  # type: ignore[arg-type]


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-only-key")
    for k in ("PROVIDER", "MODEL", "TIMEOUT_SECONDS", "MAX_OUTPUT_TOKENS"):
        monkeypatch.delenv(f"DODEAL_LLM_{k}", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(f"DODEAL_LLM_{k}", v)
    return _build_settings(_env_file=None)


def _clear_factory_cache() -> None:
    cache_clear = getattr(get_llm_client, "cache_clear", None)
    if cache_clear is not None:
        cache_clear()


# 1 -------------------------------------------------------------------------
def test_response_is_frozen_and_totals_tokens() -> None:
    r = _response()
    assert r.total_tokens == 150
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.model = "other"  # type: ignore[misc]


def test_response_repr_excludes_text() -> None:
    r = _response(text="SECRET note body with phone 0100-000-0000")
    assert "SECRET" not in repr(r)
    assert "0100" not in repr(r)


def test_provider_request_id_defaults_to_none() -> None:
    assert _response().provider_request_id is None


# 2 -------------------------------------------------------------------------
def test_finish_reason_has_exactly_three_members() -> None:
    assert {m.name for m in FinishReason} == {"STOP", "MAX_TOKENS", "OTHER"}


# 3 -------------------------------------------------------------------------
@pytest.mark.parametrize("reason", list(LLMErrorReason))
def test_provider_error_message_is_fixed_per_reason(reason: LLMErrorReason) -> None:
    err = LLMProviderError(reason, transient=True)
    assert str(err) == f"llm_provider_error:{reason.value}"
    assert err.reason is reason
    assert err.transient is True


def test_provider_error_carries_transient_false() -> None:
    assert LLMProviderError(LLMErrorReason.AUTH, transient=False).transient is False


# 4 -------------------------------------------------------------------------
def test_settings_parse_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(
        monkeypatch,
        PROVIDER="anthropic",
        MODEL="pinned-id-2026-01-01",
        TIMEOUT_SECONDS="45.5",
        MAX_OUTPUT_TOKENS="2048",
    )
    assert s.llm_provider is LLMProvider.ANTHROPIC
    assert s.llm_model == "pinned-id-2026-01-01"
    assert s.llm_timeout_seconds == 45.5
    assert s.llm_max_output_tokens == 2048


def test_invalid_provider_fails_closed_at_settings_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError):
        _settings(monkeypatch, PROVIDER="anthropc")


def test_llm_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert s.llm_provider is None
    assert s.llm_model == ""
    assert s.llm_max_output_tokens == 1024


# 5 -------------------------------------------------------------------------
def test_llm_timeout_is_separate_from_global(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert s.llm_timeout_seconds == 60.0
    assert s.llm_timeout_seconds != s.external_call_timeout_seconds


# 6 -------------------------------------------------------------------------
# The factory READS the client lifespan built; it never builds one. Settings do
# not reach it at all any more (item 84), so the cases that used to be about
# provider/model config now live on build_llm_client in
# tests/unit/test_openai_compatible_adapter.py.


class _FakeState:
    """Stands in for app.state, which is a plain attribute bag."""

    def __init__(self, **attrs: object) -> None:
        self.__dict__.update(attrs)


class _FakeApp:
    def __init__(self, state: _FakeState) -> None:
        self.state = state


class _FakeRequest:
    def __init__(self, state: _FakeState) -> None:
        self.app = _FakeApp(state)


def test_factory_returns_the_client_lifespan_built() -> None:
    built = _StructuralClient()
    request = _FakeRequest(_FakeState(llm=built))
    assert get_llm_client(request) is built  # type: ignore[arg-type]


def test_factory_refuses_when_no_client_was_built() -> None:
    """Provider unset: the app started (permissively), but a judgement cannot."""
    request = _FakeRequest(_FakeState(llm=None))
    with pytest.raises(LLMConfigurationError) as exc:
        get_llm_client(request)  # type: ignore[arg-type]
    assert str(exc.value) == "llm_not_configured"


def test_factory_refuses_when_lifespan_never_ran() -> None:
    """No `llm` attribute at all -- an app assembled without its lifespan must
    not read as configured just because the attribute is missing."""
    request = _FakeRequest(_FakeState())
    with pytest.raises(LLMConfigurationError):
        get_llm_client(request)  # type: ignore[arg-type]


def test_factory_never_builds_a_client_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One pool per process. A factory that built would open a fresh connection
    pool per request and re-run the startup profile sweep on every judgement."""
    monkeypatch.setattr(
        llm_pkg,
        "build_llm_client",
        lambda *a, **k: pytest.fail("the dependency built a client"),
    )
    built = _StructuralClient()
    assert get_llm_client(_FakeRequest(_FakeState(llm=built))) is built  # type: ignore[arg-type]


# 7 -------------------------------------------------------------------------
class _StructuralClient:
    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        return _response()


class _WrongShape:
    async def generate(self, prompt: AssembledPrompt) -> LLMResponse:
        return _response()


def test_runtime_checkable_protocol() -> None:
    assert isinstance(_StructuralClient(), LLMClient)
    assert not isinstance(_WrongShape(), LLMClient)


# 8 -------------------------------------------------------------------------
def test_prompt_parameter_type_matches_builder_return() -> None:
    hints = typing.get_type_hints(LLMClient.complete)
    assert hints["prompt"] is AssembledPrompt
    assert typing.get_type_hints(build_prompt)["return"] is AssembledPrompt


# 9 -------------------------------------------------------------------------
def test_profile_is_a_required_keyword_on_the_protocol() -> None:
    """Piece M's one interface change: keyword-only, required, and a str."""
    parameter = inspect.signature(LLMClient.complete).parameters["profile"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    assert typing.get_type_hints(LLMClient.complete)["profile"] is str


def test_the_protocol_still_has_exactly_one_method() -> None:
    # The seam grew a keyword, not a surface. A second method is how a caller
    # starts choosing a provider.
    methods = [
        name
        for name in LLMClient.__protocol_attrs__  # type: ignore[attr-defined]
        if not name.startswith("_")
    ]
    assert methods == ["complete"]
