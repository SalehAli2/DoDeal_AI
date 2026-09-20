"""FakeLLM satisfies the seam and behaves like a strict script.

The template-directed half of the file is about ONE hazard: vague detection and
scoring are gathered, so which of them reaches the fake first is a scheduling
accident, and a positional script quietly depends on it. Those tests run the
real `detect_vagueness` and `score_note` under a real `asyncio.gather`, in both
argument orders, because a fake that only worked in the order the pipeline
happens to use today would be exactly the bug this piece exists to remove.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import FinishReason, LLMClient, LLMErrorReason, LLMProviderError
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_SCORE
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.schemas import CheckName, NoteType
from dodeal_ai.units.structured_intelligence.scoring import (
    SCORE_TEMPLATE,
    build_score_prompt,
    score_note,
)
from dodeal_ai.units.structured_intelligence.vague import detect_vagueness, template_for
from tests.helpers.fake_leads import note
from tests.helpers.fake_llm import (
    FakeLLM,
    FakeLLMExhausted,
    json_response,
    response,
    truncated,
)
from tests.helpers.scopes import TEST_SCOPE
from tests.helpers.score_answers import score_payload

PROMPT = AssembledPrompt(stable="S", variable="V")
# These tests are about the SCRIPT, not about which profile was named --
# `profile` is required on the seam, so one real name serves throughout.
PROFILE = PROFILE_UNIT_A_SCORE

CONFIG = get_tenant_config("tenant-a")
NOTE_TYPE = NoteType.DISCOVERY
NOTE = note(10, "Called the client, discussed the New Cairo 3BR, following up Tuesday.")
VAGUE_TEMPLATE = template_for(NOTE_TYPE)

VAGUE_ANSWER = {
    "is_vague": True,
    "missing_components": ["next_step_with_date"],
    "clarification_prompt": "When are you following up with this client?",
    "reasoning": "No date was given for the next step.",
}
# The nine checks applicable to a discovery note under the shipped config
# (deal_specifics is suppressed by Q13, so its three are not asked).
SCORE_ANSWER = score_payload()


def test_satisfies_protocol_statically_and_at_runtime() -> None:
    client: LLMClient = FakeLLM()  # mypy checks the signature here
    assert isinstance(client, LLMClient)


async def test_pops_responses_in_order_and_counts_calls() -> None:
    fake = FakeLLM(response("one"), response("two"))
    assert (await fake.complete(PROMPT, profile=PROFILE)).text == "one"
    assert (await fake.complete(PROMPT, profile=PROFILE)).text == "two"
    assert fake.call_count == 2


async def test_records_prompt_object_unchanged_and_kwargs() -> None:
    fake = FakeLLM(response("x"))
    await fake.complete(PROMPT, profile=PROFILE, max_output_tokens=256)
    assert fake.prompts[0] is PROMPT
    assert fake.calls[0].max_output_tokens == 256
    assert fake.calls[0].profile == PROFILE
    assert fake.profiles == [PROFILE]


async def test_default_max_output_tokens_is_none() -> None:
    fake = FakeLLM(response("x"))
    await fake.complete(PROMPT, profile=PROFILE)
    assert fake.calls[0].max_output_tokens is None


async def test_scripted_exception_is_raised_and_still_recorded() -> None:
    fake = FakeLLM(
        LLMProviderError(LLMErrorReason.RATE_LIMITED, transient=True), response("ok")
    )
    with pytest.raises(LLMProviderError):
        await fake.complete(PROMPT, profile=PROFILE)
    assert (await fake.complete(PROMPT, profile=PROFILE)).text == "ok"
    assert fake.call_count == 2


async def test_exhausted_script_raises_not_defaults() -> None:
    fake = FakeLLM(response("only"))
    await fake.complete(PROMPT, profile=PROFILE)
    with pytest.raises(FakeLLMExhausted):
        await fake.complete(PROMPT, profile=PROFILE)
    assert fake.call_count == 2  # the failing call is still recorded


async def test_script_can_be_extended_mid_test() -> None:
    fake = FakeLLM()
    fake.script(response("late"))
    assert (await fake.complete(PROMPT, profile=PROFILE)).text == "late"


def test_response_factory_carries_configurable_tokens() -> None:
    r = response("x", input_tokens=7, output_tokens=3)
    assert r.total_tokens == 10
    assert r.finish_reason is FinishReason.STOP


def test_truncated_sets_max_tokens() -> None:
    assert truncated("cut").finish_reason is FinishReason.MAX_TOKENS


# --- template-directed scripting --------------------------------------------


@pytest.mark.parametrize("score_first", [False, True])
async def test_template_answers_land_on_the_right_pass_in_either_gather_order(
    score_first: bool,
) -> None:
    fake = FakeLLM()  # no positional script at all: both answers are directed
    fake.script_for(VAGUE_TEMPLATE, json_response(VAGUE_ANSWER))
    fake.script_for(SCORE_TEMPLATE, json_response(SCORE_ANSWER))
    settings = get_settings()

    vague = detect_vagueness(
        fake, NOTE, NOTE_TYPE, config=CONFIG, scope=TEST_SCOPE, settings=settings
    )
    score = score_note(
        fake, NOTE, NOTE_TYPE, config=CONFIG, scope=TEST_SCOPE, settings=settings
    )
    # Reversing the ARGUMENTS to gather is what reverses arrival order; the
    # coroutines above have not started yet.
    if score_first:
        (score_out, _), (vague_out, _) = await asyncio.gather(score, vague)
    else:
        (vague_out, _), (score_out, _) = await asyncio.gather(vague, score)

    assert vague_out.is_vague is True
    assert score_out.checks[CheckName.WH_OUTCOME] is True
    assert fake.call_count == 2


async def test_template_queue_drains_in_order_across_a_reprompt() -> None:
    fake = FakeLLM()
    fake.script_for(SCORE_TEMPLATE, response("not json"), json_response(SCORE_ANSWER))

    out, _ = await score_note(
        fake, NOTE, NOTE_TYPE, config=CONFIG, scope=TEST_SCOPE, settings=get_settings()
    )

    assert out.checks[CheckName.WH_OUTCOME] is True
    assert fake.call_count == 2
    # Why the second call stayed on this queue: with_tail changes the tail and
    # nothing else, so `stable` -- the key -- is the same string both times.
    assert fake.prompts[1].stable == fake.prompts[0].stable
    assert fake.prompts[1].tail


async def test_exhausted_template_queue_names_the_template_and_never_falls_back() -> (
    None
):
    # A positional answer IS available. The empty queue must still raise: a
    # fall-through would answer a scoring call with someone else's response.
    fake = FakeLLM(json_response(SCORE_ANSWER))
    fake.script_for(SCORE_TEMPLATE, json_response(SCORE_ANSWER))
    prompt = build_score_prompt(NOTE, NOTE_TYPE, CONFIG)

    await fake.complete(prompt, profile=PROFILE)
    with pytest.raises(FakeLLMExhausted, match=re.escape(SCORE_TEMPLATE)):
        await fake.complete(prompt, profile=PROFILE)

    assert fake.call_count == 2  # the failing call is still recorded


async def test_positional_still_serves_a_prompt_with_no_template_queue() -> None:
    fake = FakeLLM(response("one"), response("two"))
    fake.script_for(SCORE_TEMPLATE, json_response(SCORE_ANSWER))

    # PROMPT.stable is "S": no queue, so the positional script serves it.
    assert (await fake.complete(PROMPT, profile=PROFILE)).text == "one"
    assert (await fake.complete(PROMPT, profile=PROFILE)).text == "two"
    # ...and it drew from the positional script only -- the scoring queue is
    # still full.
    scored = await fake.complete(
        build_score_prompt(NOTE, NOTE_TYPE, CONFIG), profile=PROFILE
    )
    assert "checks" in scored.text


def test_still_satisfies_llmclient_with_a_template_queue_in_use() -> None:
    fake = FakeLLM()
    fake.script_for(SCORE_TEMPLATE, response("x"))
    client: LLMClient = fake  # mypy checks the signature here
    assert isinstance(client, LLMClient)
