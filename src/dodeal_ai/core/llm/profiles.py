"""Named model profiles — the ONE vocabulary of task names on the seam.

A profile name says WHICH TASK is calling; the profile table says what that task
runs on. The caller names the profile and nothing else (report R17): it never
sees a provider, a model id or a temperature, so a per-task model choice is a
config change and not a code change.

Resolution lives here rather than at the call site because the call site has no
settings to resolve against in a test -- every test injects FakeLLM through
dependency_overrides[get_llm_client], with no provider configured. The adapter
(item 76) is what calls resolve_profile(); until it exists this module is the
resolution rule plus the names, and both are tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dodeal_ai.core.config import LLMProvider, ModelProfile, Settings
from dodeal_ai.core.llm.client import LLMConfigurationError

# Register item 116: every Unit A ceiling (classify.py, vague.py, scoring.py)
# assumes a NON-REASONING model; a profile pointing a task at a reasoning model
# needs its ceiling re-sized first, or the hidden reasoning truncates the answer.
#
# The only names Unit A may pass. A fourth task means a fourth constant here
# first, which is the point: KNOWN_PROFILES is grepped against the units.
PROFILE_UNIT_A_CLASSIFY = "unit_a.classify"
PROFILE_UNIT_A_VAGUE = "unit_a.vague"
PROFILE_UNIT_A_SCORE = "unit_a.score"

# Unit B's two call passes (units/call_intelligence/passes.py). Their ceilings
# assume a NON-REASONING model too (register item 116).
PROFILE_UNIT_B_EXTRACT = "unit_b.extract"
PROFILE_UNIT_B_PROSE = "unit_b.prose"

# Unit B's wave 2 passes, one profile each (units/call_intelligence/wave2.py).
PROFILE_UNIT_B_OBJECTIONS = "unit_b.objections"

KNOWN_PROFILES: tuple[str, ...] = (
    PROFILE_UNIT_A_CLASSIFY,
    PROFILE_UNIT_A_VAGUE,
    PROFILE_UNIT_A_SCORE,
    PROFILE_UNIT_B_EXTRACT,
    PROFILE_UNIT_B_PROSE,
    PROFILE_UNIT_B_OBJECTIONS,
)


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """What one profile name resolved to: everything an adapter needs to place a
    call, and nothing a caller needs to know."""

    provider: LLMProvider
    model: str
    temperature: float
    max_output_tokens: int | None
    # Register item 147, each sent only when set; the defaults send nothing new.
    reasoning_effort: Literal["low", "medium", "high"] | None = None
    response_format: Literal["json_object", "json_schema"] = "json_object"
    seed: int | None = None

    @property
    def reasoning(self) -> bool:
        """Whether the model reasons, spending hidden tokens inside its ceiling."""
        return self.reasoning_effort is not None

    def effective_max_output_tokens(self, task_ceiling: int) -> int:
        """THE CEILING RULE: a profile may LOWER a task's ceiling, never raise
        it. The task constant is sized against the longest answer the task can
        produce (llm_call.complete_once); a profile that raised it would spend
        money the task cannot use, and one that lowers it is a deliberate cap.
        """
        if self.max_output_tokens is None:
            return task_ceiling
        return min(task_ceiling, self.max_output_tokens)


def resolve_profile(settings: Settings, name: str) -> ResolvedProfile:
    """The profile `name` resolves to, or the single-model fallback.

    THE FALLBACK RULE, in one sentence: a name that is not in llm_profiles
    resolves to the llm_provider/llm_model pair at temperature 0 with no ceiling
    of its own, no reasoning, JSON object mode and no seed -- so a deployment
    that runs one model configures the pair it already had and never writes a
    profile at all.

    Unknown name AND no fallback pair is the same failure get_llm_client()
    reports, with the same fixed message (`llm_not_configured`): the seam was
    asked for a model without being told which one.
    """
    profile: ModelProfile | None = settings.llm_profiles.get(name)
    if profile is not None:
        return ResolvedProfile(
            provider=profile.provider,
            model=profile.model,
            temperature=profile.temperature,
            max_output_tokens=profile.max_output_tokens,
            reasoning_effort=profile.reasoning_effort,
            response_format=profile.response_format,
            seed=profile.seed,
        )
    if settings.llm_provider is None or not settings.llm_model:
        raise LLMConfigurationError()
    return ResolvedProfile(
        provider=settings.llm_provider,
        model=settings.llm_model,
        temperature=0.0,
        max_output_tokens=None,
    )
