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
from dodeal_ai.core.llm.fallback import FallbackLLMClient
from dodeal_ai.core.llm.openai_compatible import (
    BASE_URLS,
    OpenAICompatibleClient,
    OpenAICompatibleError,
)
from dodeal_ai.core.llm.routing import (
    DEFAULT_ROUTE,
    ModelRouter,
    build_router,
    route_names,
    routed,
)

__all__ = [
    "DEFAULT_ROUTE",
    "FallbackLLMClient",
    "FinishReason",
    "LLMClient",
    "LLMConfigurationError",
    "LLMErrorReason",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "ModelRouter",
    "OpenAICompatibleClient",
    "OpenAICompatibleError",
    "aclose_llm",
    "build_llm_client",
    "build_router",
    "get_llm_client",
    "route_names",
    "routed",
]


def build_llm_client(settings: Settings, http: httpx.AsyncClient) -> LLMClient:
    """The provider client `settings` describes, or a fail-closed ConfigError.

    Takes its two dependencies as ARGUMENTS rather than reading them: lifespan
    (item 84) owns the pooled AsyncClient and builds this once at startup, so a
    ConfigError is a refusal to start rather than a 500 on the first judgement.
    get_llm_client() below is the request-scoped dependency; it reads the
    client lifespan built here and never builds one.

    Every refusal NAMES THE PROVIDER and never a key value -- a message that
    quoted the key would put it in the startup log, which is the one log line
    everybody pastes into a ticket.
    """
    provider = settings.llm_provider
    if provider is None or not settings.llm_model:
        # The same failure, with the same fixed message, that get_llm_client
        # reports: the seam was asked for a model without being told which one.
        raise LLMConfigurationError()
    primary = _build_one(settings, http, prefix="llm")
    fallback_settings = _fallback_settings(settings)
    if fallback_settings is None:
        return primary
    if not fallback_settings.llm_model:
        raise ConfigError("llm_fallback_not_configured")
    # Validated exactly like the primary (register item 21), on its own settings.
    return FallbackLLMClient(
        primary, _build_one(fallback_settings, http, prefix="llm_fallback")
    )


async def aclose_llm(client: LLMClient | None) -> None:
    """Close the pools a router owns; the pair's pool is its builder's."""
    if isinstance(client, ModelRouter):
        await client.aclose()


def _build_one(
    settings: Settings, http: httpx.AsyncClient, *, prefix: str
) -> OpenAICompatibleClient:
    """One provider client from `settings`'s llm_* fields, or a ConfigError
    whose code starts with `prefix` and names the provider, never a value."""
    provider = settings.llm_provider
    assert provider is not None  # both callers checked
    if provider not in BASE_URLS:
        # anthropic and gemini parse but have no adapter; one is added when
        # a deployment needs that vendor.
        # Named rather than silently unsupported: a deployment that set this on
        # purpose deserves to be told which half is missing.
        raise ConfigError(f"{prefix}_provider_not_supported:{provider.value}")
    if settings.llm_api_key is None:
        raise ConfigError(f"{prefix}_api_key_missing:{provider.value}")
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
    # cannot disagree with what a real call would do. A profile naming a
    # registry provider is that provider's to sweep (core/llm/routing.py).
    for name, profile in settings.llm_profiles.items():
        if isinstance(profile.provider, LLMProvider):
            client.validate_profile(name)
    return client


def _fallback_settings(settings: Settings) -> Settings | None:
    """The settings the fallback client is built from, or None for no fallback.

    Its four fields stand in for the primary's, and the profile table is left
    out: it names the primary's models. Any fallback field set without a
    provider refuses to start rather than quietly leave no fallback.
    """
    provider = settings.llm_fallback_provider
    if provider is None:
        if (
            settings.llm_fallback_model
            or settings.llm_fallback_api_key is not None
            or settings.llm_fallback_base_url is not None
        ):
            raise ConfigError("llm_fallback_provider_missing")
        return None
    return settings.model_copy(
        update={
            "llm_provider": provider,
            "llm_model": settings.llm_fallback_model,
            "llm_api_key": settings.llm_fallback_api_key,
            "llm_base_url": settings.llm_fallback_base_url,
            "llm_profiles": {},
        }
    )


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
