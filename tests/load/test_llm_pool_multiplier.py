"""Register item 114: the model pool is sized for the passes a judgement gathers.

`max_inflight` judgements run at once through a FakeLLM that holds every call for
HOLD_SECONDS and records the most calls in flight together. Classify runs alone
and vague and score are gathered, so the peak may reach twice the in-flight cap
and never pass it, and `_llm_limits` must size the pool to exactly that. The
measured peak is recorded on the test as the user property `llm_peak_inflight`.
"""

from __future__ import annotations

import asyncio

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import LLMResponse, get_llm_client
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.main import _llm_limits, app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.load.conftest import CLASSIFY_AS, SCORE_ANSWER

# The passes one judgement issues together: vague detection and scoring.
GATHERED_PASSES = 2
# Long enough for a whole burst's calls to overlap, short enough to cost nothing.
HOLD_SECONDS = 0.05
NOT_VAGUE_ANSWER = {
    "is_vague": False,
    "missing_components": [],
    "clarification_prompt": None,
    "reasoning": "The note says what happened and when the follow-up is.",
}


class PeakLLM(FakeLLM):
    """A FakeLLM that holds each call and records the most in flight at once."""

    def __init__(self) -> None:
        super().__init__()
        self.inflight = 0
        self.peak = 0

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(HOLD_SECONDS)
            return await super().complete(
                prompt, profile=profile, max_output_tokens=max_output_tokens
            )
        finally:
            self.inflight -= 1


async def test_the_model_pool_covers_every_call_a_full_burst_holds_at_once(
    lane, every_usable_note, request
):
    """A burst of max_inflight non-vague judgements never holds more than twice that many model calls at once, and the model pool is exactly that size."""
    settings = get_settings()
    burst = settings.max_inflight
    picked = every_usable_note[:burst]
    assert len(picked) == burst
    llm = PeakLLM()
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *[json_response({"note_type": CLASSIFY_AS.value})] * burst,
    )
    llm.script_for(
        template_for(CLASSIFY_AS), *[json_response(NOT_VAGUE_ANSWER)] * burst
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(SCORE_ANSWER)] * burst)
    app.dependency_overrides[get_llm_client] = lambda: llm

    responses = await asyncio.gather(
        *(lane.judge(lead_id, note.id) for lead_id, note in picked)
    )
    request.node.user_properties.append(("llm_peak_inflight", llm.peak))
    # Read out before asserting, so a failure prints a number and not the
    # Settings repr, which carries the signing key.
    pool_size = _llm_limits(settings).max_connections

    assert [r.status_code for r in responses] == [200] * burst
    assert [r.json()["analysis"]["is_vague"] for r in responses] == [False] * burst
    assert llm.call_count == 3 * burst
    assert llm.peak <= burst * GATHERED_PASSES
    assert pool_size == burst * GATHERED_PASSES
