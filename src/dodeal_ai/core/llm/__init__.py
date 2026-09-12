"""core/llm — provider abstraction. See client.py for what the seam is and is
not. The gateway (routing, fallback, breakers) grows here, behind
get_llm_client(); the Protocol stays one method."""

from __future__ import annotations

import httpx
from fastapi import Request

from dodeal_ai.core.config import ConfigError, LLMProvider, Settings
from dodeal_ai.core.llm.client import (
    FinishReason,
    LLMClient,
    LLMConfigurationError,
    LLMErrorReason,
    LLMProviderError,
    LLMResponse,
)
from dodeal_ai.core.llm.openai_compatible import (
    BASE_URLS,
    OpenAICompatibleClient,
    OpenAICompatibleError,
)

__all__ = [
    "FinishReason",
    "LLMClient",
    "LLMConfigurationError",
    "LLMErrorReason",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "OpenAICompatibleClient",
    "OpenAICompatibleError",
    "build_llm_client",
    "get_llm_client",
]


def build_llm_client(settings: Settings, http: httpx.AsyncClient) -> LLMClient:
    """The provider client `settings` describes, or a fail-closed ConfigError.

    Takes its two dependencies as ARGUMENTS rather than reading them: lifespan
    (item 84) owns the pooled AsyncClient and builds this once at startup, so a
    ConfigError is a refusal to start rather than a 500 on the first judgement.
    get_llm_client() below is the request-scoped dependency and is unchanged
    until 76.2 wires the two together.

    Every refusal NAMES THE PROVIDER and never a key value -- a message that
    quoted the key would put it in the startup log, which is the one log line
    everybody pastes into a ticket.
    """
    provider = settings.llm_provider
    if provider is None or not settings.llm_model:
        # The same failure, with the same fixed message, that get_llm_client
        # reports: the seam was asked for a model without being told which one.
        raise LLMConfigurationError()
    if provider not in BASE_URLS:
        # anthropic and gemini parse but have no adapter yet (gemini is 76.3).
        # Named rather than silently unsupported: a deployment that set this on
        # purpose deserves to be told which half is missing.
        raise ConfigError(f"llm_provider_not_supported:{provider.value}")
    if settings.llm_api_key is None:
        raise ConfigError(f"llm_api_key_missing:{provider.value}")
    client = OpenAICompatibleClient(
        settings.llm_base_url or BASE_URLS[provider],
        settings.llm_model,
        settings.llm_api_key,
        http,
        settings=settings,
    )
    # THE STARTUP SWEEP. Every configured profile is resolved once, here, so a
    # bad temperature or a profile naming another vendor refuses to start --
    # instead of 503ing the first judgement that names it, which could be days
    # later and on one task only. Resolution is where both guards live, so this
    # cannot disagree with what a real call would do.
    for name in settings.llm_profiles:
        client.validate_profile(name)
    return client


def get_llm_client(request: Request) -> LLMClient:
    """FastAPI dependency: the ONE client lifespan built, per request.

    It READS `app.state.llm` and never builds -- building here would open a
    fresh connection pool per request, which is the pooling bug the lifespan
    exists to avoid, and would re-run the profile sweep on every judgement.

    `None` means the app started without a provider configured (the permissive
    startup rule in main.py). That is a fail-closed 503 at the route, the same
    answer `/ready` is already giving the orchestrator.

    Tests override it at the route (app.dependency_overrides[get_llm_client]),
    which is signature-agnostic -- there is no test-mode switch here and must
    never be one.
    """
    client: LLMClient | None = getattr(request.app.state, "llm", None)
    if client is None:
        raise LLMConfigurationError()
    return client
