"""The reprompt (A6): the second call carries a fixed tail file and never a
word of the answer it rejected; coaching_v5 says a moment has no quote."""

from __future__ import annotations

from dodeal_ai.core import prompting
from dodeal_ai.core.prompting import build_prompt
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_passes import _extract, _extraction

# Words no transcript or template holds: only the rejected answer does.
MARKERS = ("Zanzibar-rejected-wanted", "Quokka-rejected-budget")


async def test_the_tail_holds_no_text_from_the_rejected_answer() -> None:
    rejected = _extraction(
        budget={"value": MARKERS[1], "state": "not_mentioned", "quote": None, "segment": None}
    )  # fmt: skip
    rejected["wanted"]["text"] = MARKERS[0]
    llm = FakeLLM(json_response(rejected), json_response(_extraction()))

    await _extract(llm)

    assert llm.call_count == 2
    second = llm.calls[1].prompt
    fixed = {
        (prompting._DEFAULT_PROMPTS_DIR / name).read_text(encoding="utf-8").strip()
        for name in (REPROMPT_TAIL_TEMPLATE, *REPROMPT_TAILS.values())
    }
    assert second.tail.strip() in fixed
    for marker in MARKERS:
        assert marker not in second.text


def test_the_coaching_prompt_says_a_moment_has_no_quote() -> None:
    assert COACHING_TEMPLATE == "call_intelligence/coaching_v5.txt"
    text = " ".join(build_prompt(COACHING_TEMPLATE, caller_data="").stable.split())
    assert "A moment has no quote" in text
