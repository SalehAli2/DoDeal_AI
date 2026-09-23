"""The fallback provider (register item 21): one second chance, and a narrow one.

`FallbackLLMClient` sends every call to the primary. Only when the primary gave
no response body at all -- a connect error, a 429, a 503, or its breaker open --
is the same prompt sent to the fallback, once. A timeout may already be billed
and a malformed 200 was answered, so neither is ever sent twice. The answering
response is returned as it came, so model_version names the model that ran.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from dodeal_ai.core.llm.client import LLMResponse
from dodeal_ai.core.llm.openai_compatible import (
    OpenAICompatibleClient,
    OpenAICompatibleError,
)
from dodeal_ai.core.prompting import AssembledPrompt

_logger = logging.getLogger("dodeal_ai.llm.fallback")


class FallbackLLMClient:
    """Structurally an LLMClient: the primary, and the fallback behind it."""

    def __init__(
        self, primary: OpenAICompatibleClient, fallback: OpenAICompatibleClient
    ) -> None:
        self._primary = primary
        self._fallback = fallback

    @property
    def primary(self) -> OpenAICompatibleClient:
        return self._primary

    @property
    def fallback(self) -> OpenAICompatibleClient:
        return self._fallback

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        try:
            return await self._primary.complete(
                prompt,
                profile=profile,
                max_output_tokens=max_output_tokens,
                response_schema=response_schema,
            )
        except OpenAICompatibleError as exc:
            if not exc.fallback_eligible:
                raise
            # The primary's reason only: a fixed vocabulary, never its message.
            _logger.warning(
                "llm_fallback_used",
                extra={
                    "reason_code": "llm_fallback_used",
                    "primary_reason": exc.reason.value,
                },
            )
        return await self._fallback.complete(
            prompt,
            profile=profile,
            max_output_tokens=max_output_tokens,
            response_schema=response_schema,
        )
