"""The reprompt (A6, D-84): the second call carries its fixed tail file and
then the failed fields, each by path and code, and never a word of the answer
it rejected -- not a quote, not a value, not a key the model made up;
coaching_v5 says a moment has no quote."""

from __future__ import annotations

from dodeal_ai.core.prompting import build_prompt
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    QUOTE_EXACT_TAIL_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.reprompt_tails import tail_with
from tests.unit.test_call_passes import (
    _extract,
    _extraction,
    _rejects_once,
    _reprompted_tail,
)

# Words no transcript, template or schema holds: only the rejected answer does.
MARKERS = ("Zanzibar rejected wanted", "Quokka-rejected-budget", "Wombat_rejected_key")


async def test_the_tail_names_the_failed_fields_and_no_text_of_the_answer() -> None:
    """The guard: an invented quote and a value where none was mentioned are
    named by path and code; neither the quote nor the value is sent back."""
    rejected = _extraction(
        budget={"value": MARKERS[1], "state": "not_mentioned", "quote": None, "segment": None}
    )  # fmt: skip
    rejected["wanted"]["quote"] = MARKERS[0]
    llm = FakeLLM(json_response(rejected), json_response(_extraction()))

    await _extract(llm)

    assert llm.call_count == 2
    second = llm.calls[1].prompt
    assert second.tail == tail_with(
        QUOTE_EXACT_TAIL_TEMPLATE,
        "$.details.budget: not_mentioned_with_value",
        "$.wanted: quote_not_in_segment",
    )
    for marker in MARKERS:
        assert marker not in second.text


async def test_a_key_the_model_invented_is_never_written_back() -> None:
    """A key outside the schema fails as extra_forbidden; its name is the
    model's text, so the path says * in its place."""
    rejected = _extraction()
    rejected[MARKERS[2]] = MARKERS[1]
    llm = FakeLLM(json_response(rejected), json_response(_extraction()))

    await _extract(llm)

    second = llm.calls[1].prompt
    assert second.tail == tail_with(REPROMPT_TAIL_TEMPLATE, "$.*: extra_forbidden")
    for marker in MARKERS:
        assert marker not in second.text


async def test_a_path_carrying_answer_text_is_written_as_a_star() -> None:
    """Were a check ever to put the answer's words in a path, only the
    schema's field names and list indexes would still be written."""
    errors = ((f"wanted.{MARKERS[0]}", "quote_not_in_segment"), ("agreed.0", "x-1"))
    tail = await _reprompted_tail(_rejects_once(*errors))
    assert tail == tail_with(
        QUOTE_EXACT_TAIL_TEMPLATE, "$.wanted.*: quote_not_in_segment", "$.agreed.0: *"
    )
    assert MARKERS[0] not in tail


def test_the_coaching_prompt_says_a_moment_has_no_quote() -> None:
    assert COACHING_TEMPLATE == "call_intelligence/coaching_v5.txt"
    text = " ".join(build_prompt(COACHING_TEMPLATE, caller_data="").stable.split())
    assert "A moment has no quote" in text
