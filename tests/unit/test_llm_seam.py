"""Step 1 — the LLM seam. Hermetic: no network, no provider SDK."""

from __future__ import annotations

import dataclasses
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
@pytest.mark.parametrize(
    ("overrides", "expected", "message"),
    [
        ({}, LLMConfigurationError, "llm_not_configured"),
        ({"llm_provider": "anthropic"}, LLMConfigurationError, "llm_not_configured"),
        ({"llm_model": "pinned"}, LLMConfigurationError, "llm_not_configured"),
        (
            {"llm_provider": "anthropic", "llm_model": "pinned"},
            NotImplementedError,
            "llm_provider_not_wired",
        ),
    ],
)
def test_factory_behaviour(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, str],
    expected: type[Exception],
    message: str,
) -> None:
    settings = _build_settings(
        _env_file=None, jwt_signing_key="test-only-key", **overrides
    )
    monkeypatch.setattr(llm_pkg, "get_settings", lambda: settings)
    _clear_factory_cache()
    with pytest.raises(expected) as exc:
        get_llm_client()
    assert str(exc.value) == message


# 7 -------------------------------------------------------------------------
class _StructuralClient:
    async def complete(
        self, prompt: AssembledPrompt, *, max_output_tokens: int | None = None
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
