"""FakeLLM — the scripted stand-in for LLMClient in the hermetic suite.

Lives here, not inside one test module, because unit tests, the reprompt tests
(Step 10) and later security tests all need it. It is injected ONLY through
app.dependency_overrides[get_llm_client]; the factory has no test switch and
must never grow one.

No sleeping, no network, no randomness. Responses are popped in order. An
exhausted script is a test bug and raises — it never returns a default, so a
path that calls the model more times than the test expected fails loudly.

TWO WAYS TO SCRIPT IT, AND WHY THE SECOND EXISTS. The positional script
(constructor, `script`, `rescript`) answers calls in arrival order, which is
exact and readable for a test that knows the order. But vague detection and
scoring are issued together under `asyncio.gather`, and arrival order there is
a SCHEDULING ACCIDENT: it holds only because gather steps its tasks in argument
order and nothing between the pipeline and this fake ever suspends. Nothing in
the pipeline promises it, and a test that scripts two answers positionally is
silently asserting it.

So `script_for(template_name, *responses)` queues answers against the TEMPLATE
that will be sent, resolved through `build_prompt` -- the same loader the
pipeline assembles with, never a test re-reading the file. A gathered pass then
gets its own answer whichever task the loop happens to run first, and a caller
scripting a corpus does not have to know the order at all.

PRECEDENCE, in one rule: if a template queue EXISTS for `prompt.stable`, the
answer comes from it; otherwise the positional script serves the call. Existing
is not the same as non-empty -- an exhausted template queue raises rather than
falling through to the positional script, because a silent fall-through would
answer a scoring call with whatever the test had lined up for something else.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from dodeal_ai.core.llm import FinishReason, LLMResponse
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt

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


def json_response(payload: object, **kwargs: object) -> LLMResponse:
    """A reply whose text is `payload` serialised as JSON and nothing else.

    The well-behaved case: every prompt in this repo asks the model to return
    one JSON object with no prose and no code fence, so this is what "the model
    did what it was told" looks like. A test that wants the badly-behaved case
    passes the raw string to response() instead -- fenced, truncated, or not
    JSON at all -- because those are the shapes the reprompt exists for.
    """
    return response(json.dumps(payload), **kwargs)  # type: ignore[arg-type]


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


@dataclass(slots=True)
class _TemplateQueue:
    """The answers queued for one template, and the name to blame when they run
    out. Keyed by the template's stable text, but the NAME is what a failing
    test needs to read, so it is carried alongside rather than reverse-looked-up
    from a path."""

    template_name: str
    items: list[LLMResponse | BaseException] = field(default_factory=list)


class FakeLLM:
    """Structurally satisfies LLMClient. tests/helpers/test_fake_llm.py asserts
    this both for mypy (typed assignment) and at runtime (isinstance)."""

    def __init__(
        self, *script: LLMResponse | BaseException, hold_after: int | None = None
    ) -> None:
        self._script: list[LLMResponse | BaseException] = list(script)
        # stable template text -> that template's queue. Empty unless a test
        # calls script_for(), so the positional fake is untouched by this.
        self._by_template: dict[str, _TemplateQueue] = {}
        self.calls: list[RecordedCall] = []
        # Calls past this many are RECORDED and then block on `released`, so a
        # test can observe how many are in flight at once. None (the default) is
        # the ordinary fake: every call returns straight away.
        self.hold_after = hold_after
        self.released = asyncio.Event()

    def script(self, *more: LLMResponse | BaseException) -> None:
        """Append to the script mid-test (e.g. after asserting the first call)."""
        self._script.extend(more)

    def rescript(self, *script: LLMResponse | BaseException) -> None:
        """Replace the remaining script, leaving the call record alone.

        For the test that was handed a fixture-built client already wired into
        app.dependency_overrides and needs a different answer from it. The calls
        already recorded stay recorded -- this changes what happens NEXT, not
        what happened.
        """
        self._script[:] = script

    def script_for(
        self, template_name: str, *responses: LLMResponse | BaseException
    ) -> None:
        """Queue answers for whichever call is assembled from `template_name`.

        The template is resolved through `build_prompt`, so the key is the exact
        `stable` string the pipeline will produce -- including under
        `DODEAL_PROMPTS_DIR`, which a test that read the file itself would miss.
        An unknown name raises `PromptError` here, at the line that named it,
        rather than never matching a call and looking like a pipeline bug.

        A QUEUE, not one answer. The second call for a pass is the reprompt, and
        `with_tail` changes only the tail -- `stable` is carried across
        untouched -- so the reprompt lands on this same queue and pops the NEXT
        item. That is what lets a test say "malformed, then good" for one
        template without knowing where in the call order either lands.

        Calling it twice for the same template appends; it never replaces. Two
        notes' worth of answers for one template is the corpus case, and a
        second call that silently discarded the first would lose one.
        """
        stable = build_prompt(template_name, "").stable
        queue = self._by_template.get(stable)
        if queue is None:
            queue = _TemplateQueue(template_name=template_name)
            self._by_template[stable] = queue
        queue.items.extend(responses)

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
        if self.hold_after is not None and len(self.calls) > self.hold_after:
            # Recorded BEFORE the wait, so call_count reflects calls ISSUED, not
            # calls completed -- which is the whole point of the mechanism.
            await self.released.wait()
        item = self._next_for(prompt)
        if isinstance(item, BaseException):
            raise item
        return item

    def _next_for(self, prompt: AssembledPrompt) -> LLMResponse | BaseException:
        """The precedence rule, in one place: a template queue if this prompt
        has one, the positional script otherwise. An existing-but-empty queue
        raises HERE rather than falling through -- see the module docstring."""
        queue = self._by_template.get(prompt.stable)
        if queue is not None:
            if not queue.items:
                raise FakeLLMExhausted(
                    f"FakeLLM: template queue exhausted for {queue.template_name}"
                )
            return queue.items.pop(0)
        if not self._script:
            raise FakeLLMExhausted(
                f"FakeLLM: no scripted response for call {len(self.calls)}"
            )
        return self._script.pop(0)
