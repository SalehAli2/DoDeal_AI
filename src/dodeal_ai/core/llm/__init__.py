"""core/llm — provider abstraction. See client.py for what the seam is and is
not. The gateway (routing, fallback, breakers) grows here, behind
get_llm_client(); the Protocol stays one method."""

from __future__ import annotations

from dodeal_ai.core.config import LLMProvider, get_settings
from dodeal_ai.core.llm.client import (
    FinishReason,
    LLMClient,
    LLMConfigurationError,
    LLMErrorReason,
    LLMProviderError,
    LLMResponse,
)

__all__ = [
    "FinishReason",
    "LLMClient",
    "LLMConfigurationError",
    "LLMErrorReason",
    "LLMProvider",
    "LLMProviderError",
    "LLMResponse",
    "get_llm_client",
]


# CONNECT: apply the same caching decorator get_verifier() uses (lru_cache or
# none). Settings are read lazily inside — never at import.
def get_llm_client() -> LLMClient:
    """FastAPI dependency. Tests override it at the route
    (app.dependency_overrides[get_llm_client] = ...) — there is no test-mode
    switch here and must never be one."""
    settings = get_settings()
    if settings.llm_provider is None or not settings.llm_model:
        raise LLMConfigurationError()
    # Adapter lands in Step 14; until then a configured provider is loud, not silent.
    raise NotImplementedError("llm_provider_not_wired")
