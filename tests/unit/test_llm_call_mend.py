"""call_model's mend (D-101): a well-shaped answer whose evidence partly
failed is kept, the one reprompt sent, and the verified parts of both merged
-- two calls at most, nothing of either answer in the second prompt."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import MalformedOutputError
from dodeal_ai.units.call_intelligence.prompts import (
    REPROMPT_TAIL_TEMPLATE,
    build_call_prompt,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    Mend,
    call_model,
    refused_codes,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.reprompt_tails import tail_with
from tests.helpers.scopes import TEST_SCOPE

LABEL = "llm.unit_b.test"
# A word no answer's check may ever write back into a prompt.
SECRET = "zebra-quartz"


class Pair(BaseModel):
    """Two fields, each true or not; the answer's evidence, for this test."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    left: str
    right: str


def _failures(answer: Pair) -> list[tuple[str, str]]:
    return [
        (name, "quote_not_in_segment")
        for name in ("left", "right")
        if getattr(answer, name) != "true"
    ]


def _merge(first: Pair, second: Pair) -> Pair:
    update = {
        name: getattr(second, name)
        for name in ("left", "right")
        if getattr(first, name) != "true"
    }
    return first.model_copy(update=update)


MEND = Mend(_failures, _merge)


async def _call(llm: FakeLLM, *, reprompt: bool = True, mend: Mend | None = MEND):
    return await call_model(
        llm,
        build_call_prompt("call_intelligence/score_v2.txt", "TRANSCRIPT:\n"),
        Pair,
        LABEL,
        scope=TEST_SCOPE,
        settings=get_settings(),
        profile="unit_b.score",
        max_output_tokens=100,
        reprompt=reprompt,
        reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        mend=mend,
    )


async def test_an_answer_with_nothing_to_mend_costs_one_call() -> None:
    llm = FakeLLM(json_response({"left": "true", "right": "true"}))
    answer, _ = await _call(llm)
    assert (answer, llm.call_count) == (Pair(left="true", right="true"), 1)


async def test_a_partly_failing_answer_is_merged_with_the_second() -> None:
    """The guard: the first's true field kept, the second's taken only for
    what failed, the failure listed after the tail by path and code, and the
    second response is the one returned."""
    later = json_response({"left": SECRET, "right": "true"})
    llm = FakeLLM(json_response({"left": "true", "right": SECRET}), later)
    with refused_codes() as codes:
        answer, response = await _call(llm)
    assert answer == Pair(left="true", right="true")
    assert llm.call_count == 2
    assert response is later
    second = llm.calls[1].prompt
    assert second.tail == tail_with(
        REPROMPT_TAIL_TEMPLATE, "$.right: quote_not_in_segment"
    )
    assert SECRET not in second.text
    assert codes == ["quote_not_in_segment"]


async def test_an_unshaped_second_answer_leaves_the_first() -> None:
    kept = json_response({"left": "true", "right": SECRET})
    llm = FakeLLM(kept, json_response({"left": "true"}))
    answer, response = await _call(llm)
    assert answer == Pair(left="true", right=SECRET)
    assert llm.call_count == 2
    assert response is kept


async def test_with_the_reprompt_withheld_the_first_answer_is_kept() -> None:
    llm = FakeLLM(json_response({"left": SECRET, "right": "true"}))
    answer, _ = await _call(llm, reprompt=False)
    assert (answer.left, llm.call_count) == (SECRET, 1)


async def test_an_unshaped_first_answer_is_reprompted_as_before() -> None:
    """Mend changes nothing for an answer the schema refused: one reprompt,
    the second answer returned as it is, whatever mend would say of it."""
    llm = FakeLLM(
        json_response({"left": "true"}),
        json_response({"left": SECRET, "right": "true"}),
    )
    answer, _ = await _call(llm)
    assert (answer, llm.call_count) == (Pair(left=SECRET, right="true"), 2)


async def test_without_mend_an_unshaped_answer_twice_is_malformed() -> None:
    llm = FakeLLM(json_response({}), json_response({}))
    with pytest.raises(MalformedOutputError):
        await _call(llm, mend=None)
    assert llm.call_count == 2
