"""FakeLLM — the scripted stand-in for LLMClient in the hermetic suite.

Lives here, not inside one test module, because unit tests, the reprompt tests
(Step 10) and later security tests all need it. It is injected ONLY through
app.dependency_overrides[get_llm_client]; the factory has no test switch and
must never grow one.

No sleeping, no network, no randomness. Responses are popped in order. An
exhausted script is a test bug and raises — it never returns a default, so a
path that calls the model more times than the test expected fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass

from dodeal_ai.core.llm import FinishReason, LLMResponse
from dodeal_ai.core.prompting import AssembledPrompt

FAKE_MODEL = "fake-model-pinned"


def response(
    text: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
    finish_reason: FinishReason = FinishReason.STOP,
    model: str = FAKE_MODEL,
    provider_request_id: str | None = None,
) -> LLMResponse:
    """Build a scripted reply. Defaults are deliberately round so a cost test
    can compute the expected charge by eye."""
    return LLMResponse(
        text=text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        model=model,
        finish_reason=finish_reason,
        provider_request_id=provider_request_id,
    )


def truncated(
    text: str,
    *,
    input_tokens: int = 100,
    output_tokens: int = 20,
) -> LLMResponse:
    """A reply cut off at the output ceiling — the likeliest malformed case."""
    return response(
        text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        finish_reason=FinishReason.MAX_TOKENS,
    )


class FakeLLMExhausted(AssertionError):
    """More calls were made than the test scripted."""


@dataclass(frozen=True, slots=True)
class RecordedCall:
    prompt: AssembledPrompt
    max_output_tokens: int | None


class FakeLLM:
    """Structurally satisfies LLMClient. tests/helpers/test_fake_llm.py asserts
    this both for mypy (typed assignment) and at runtime (isinstance)."""

    def __init__(self, *script: LLMResponse | BaseException) -> None:
        self._script: list[LLMResponse | BaseException] = list(script)
        self.calls: list[RecordedCall] = []

    def script(self, *more: LLMResponse | BaseException) -> None:
        """Append to the script mid-test (e.g. after asserting the first call)."""
        self._script.extend(more)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def prompts(self) -> list[AssembledPrompt]:
        return [c.prompt for c in self.calls]

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        self.calls.append(
            RecordedCall(prompt=prompt, max_output_tokens=max_output_tokens)
        )
        if not self._script:
            raise FakeLLMExhausted(
                f"FakeLLM: no scripted response for call {len(self.calls)}"
            )
        item = self._script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item
