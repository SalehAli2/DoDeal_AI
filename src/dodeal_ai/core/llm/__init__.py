"""core/llm — provider abstraction. See client.py for what the seam is and is
not. The gateway (routing, fallback, breakers) grows here, behind
get_llm_client(); the Protocol stays one method."""

from __future__ import annotations

import httpx

from dodeal_ai.core.config import ConfigError, LLMProvider, Settings, get_settings
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
    return OpenAICompatibleClient(
        settings.llm_base_url or BASE_URLS[provider],
        settings.llm_model,
        settings.llm_api_key,
        http,
        settings=settings,
    )


# CONNECT: apply the same caching decorator get_verifier() uses (lru_cache or
# none). Settings are read lazily inside — never at import.
def get_llm_client() -> LLMClient:
    """FastAPI dependency. Tests override it at the route
    (app.dependency_overrides[get_llm_client] = ...) — there is no test-mode
    switch here and must never be one."""
    settings = get_settings()
    if settings.llm_provider is None or not settings.llm_model:
        raise LLMConfigurationError()
    # The adapter exists (item 76 part 1) but nothing owns the pooled
    # AsyncClient it needs yet; lifespan builds it in 76.2 / item 84 and this
    # returns it. Until then a configured provider is loud, not silent.
    raise NotImplementedError("llm_provider_not_wired")
