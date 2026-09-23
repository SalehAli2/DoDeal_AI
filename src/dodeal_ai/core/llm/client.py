"""The LLM seam — the ONE interface every unit calls a model through.

Unit A owns this; Unit B consumes it. The Protocol is deliberately thin so a
provider swaps without touching a caller, and so the gateway concerns in
FUTURE_PATTERNS.md item 3 grow BEHIND get_llm_client(), never onto the
interface a unit types against.

Deliberately NOT on the Protocol, and where each lives instead:
  - retries and timeouts   -> core/resilience.py::call_with_watchdog, passed per
                              call with retry=False and timeout=llm_timeout_seconds
  - provider routing, fallback, circuit breaking
                           -> the factory / adapters in core/llm/, Step 14+
  - per-tenant quota       -> Gate 4 (core/cost)
  - validation             -> core/validation.py (LLMResponse.text is untrusted)
  - cost charging          -> core/cost::enforce_token_cost, after the call
  - provider, model, temperature
                           -> the profile table in core/llm/profiles.py. The
                              caller names a profile; the adapter resolves it.

The prompt argument is core/prompting.AssembledPrompt and passes through
UNCHANGED. An adapter reads .stable and .variable (for a cache breakpoint) and
never re-splits .text — parsing would move the injection boundary out of the
one module where it is tested.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from dodeal_ai.core.config import ConfigError
from dodeal_ai.core.prompting import AssembledPrompt


class FinishReason(str, Enum):
    """Why the model stopped. Adapters normalise provider strings to this;
    unknown values map to OTHER, never raise."""

    STOP = "stop"  # finished naturally
    MAX_TOKENS = "max_tokens"  # truncated at the output ceiling — Step 10
    # treats this differently from a complete-but-malformed response
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """One model reply. Field names map one-to-one onto OpenTelemetry gen_ai.*
    attributes (gen_ai.usage.input_tokens, gen_ai.response.model, ...).

    `text` is raw, untrusted, unparsed output. It is excluded from repr so it
    can never reach a log line by accident; validation happens in
    core/validation.py, not here.

    `model` is what the provider REPORTS ran, not what config asked for — it
    becomes model_version on every stored judgement.

    `provider_request_id` is safe to audit-log and is the only handle for
    reconciling a call that timed out on our side but was billed on theirs.

    `cached_input_tokens` is the PART of input_tokens the provider served from
    its prompt cache, and `reasoning_tokens` the PART of output_tokens spent on
    reasoning; 0 when the provider does not say (register item "cost").
    """

    text: str = field(repr=False)
    input_tokens: int
    output_tokens: int
    model: str
    finish_reason: FinishReason
    provider_request_id: str | None = None
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class LLMErrorReason(str, Enum):
    RATE_LIMITED = "rate_limited"
    UNAVAILABLE = "unavailable"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    UNKNOWN = "unknown"
    # Our own pool had no free connection: the provider was never asked
    # (register item 112), so this is not a timeout.
    PROVIDER_POOL_EXHAUSTED = "provider_pool_exhausted"
    # This provider's circuit breaker is open: refused with no socket opened
    # (register item 20).
    BREAKER_OPEN = "breaker_open"


class LLMProviderError(Exception):
    """A provider-side failure, translated by the adapter (Step 14).

    Not for timeouts — those are call_with_watchdog's. Callers branch on
    `transient` alone; `reason` is for the audit log.

    RULE: str(self) is a fixed string derived only from the reason. No provider
    message, no prompt, no output text is ever attached — this exception can
    surface in a log line, and the logging rule (ASSUMPTIONS §3.3) applies.
    Put provider detail in the audit log via `reason`, not in the message.
    """

    def __init__(self, reason: LLMErrorReason, *, transient: bool) -> None:
        self.reason = reason
        self.transient = transient
        super().__init__(f"llm_provider_error:{reason.value}")


class LLMConfigurationError(ConfigError):
    """The seam was asked for a client without a provider and a pinned model.

    THE one definition. A ConfigError subclass so startup/readiness treat it
    fail-closed like any other missing-config failure; the import direction is
    llm -> config, never the reverse, so it lives here and not in config.py.
    """

    def __init__(self) -> None:
        super().__init__("llm_not_configured")


@runtime_checkable
class LLMClient(Protocol):
    """Exactly one method. Async because Unit B's prompts are long."""

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
        response_schema: Mapping[str, object] | None = None,
    ) -> LLMResponse:
        """Send the assembled prompt unchanged. `profile` names the calling TASK
        (core/llm/profiles.py) and is the only thing a caller says about the
        model; None max_output_tokens = Settings.llm_max_output_tokens.
        `response_schema` is the JSON schema of the answer the task validates
        against, sent only when the profile asks for json_schema."""
