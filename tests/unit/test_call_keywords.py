"""The keyword vocabulary (keywords.py, extras.py): used after transcription --
spotted in code, and the extras pass's keywords mapped to its names."""

from __future__ import annotations

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.extras import (
    Extras,
    check_extras,
    extras_part,
    find_extras,
)
from dodeal_ai.units.call_intelligence.keywords import spot_keywords
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_extras import SCOPE, _answer, _call, _say

LISTED = frozenset({"Beachfront", "Palm Grove Residences", "داماك"})


def test_beachfront_said_in_a_segment_yields_the_listed_term_and_its_time() -> None:
    """Matched as the alarm matcher normalises, one name unbroken, no model."""
    segments = (
        _say(0, "agent", "Hello there."),
        _say(5, "lead", "We like the beachfront towers, and لداماك too."),
        _say(10, "lead", "Palm trees in the grove, residences later."),
    )
    assert spot_keywords(segments, LISTED) == [
        {"term": "Beachfront", "segment": "s2", "start_s": 5.0},
        {"term": "داماك", "segment": "s2", "start_s": 5.0},
    ]


async def test_the_extras_result_uses_the_listed_canonical_name() -> None:
    """The list reaches the prompt; a listed name is kept, an unlisted one is
    null and its keyword kept as found."""
    answer = _answer()
    answer["keywords"][0]["canonical"] = "Palm Grove Residences"
    llm = FakeLLM(json_response(answer))
    found, _ = await find_extras(
        llm, _call(), scope=SCOPE, settings=get_settings(), vocabulary=LISTED
    )
    assert "- Palm Grove Residences\n" in llm.calls[0].prompt.variable
    keywords = extras_part(_call(), found, vocabulary=LISTED)["keywords"]
    assert [k["canonical"] for k in keywords] == ["Palm Grove Residences", None]

    answer["keywords"][0]["canonical"] = "Palm Grove"
    off_list = Extras.model_validate(answer)
    check_extras(_call())(off_list)
    keywords = extras_part(_call(), off_list, vocabulary=LISTED)["keywords"]
    assert [(k["said"], k["canonical"]) for k in keywords] == [
        ("Palm Grove Residences", None),
        ("a garden", None),
    ]


def test_the_vocabulary_holds_100_distinct_names_at_most() -> None:
    assert CallsConfig(keyword_vocabulary=LISTED).keyword_vocabulary == LISTED
    for refused in (
        {f"Project {n}" for n in range(101)},
        {"Beachfront", "BEACHFRONT"},
        {"---"},
    ):
        with pytest.raises(ValueError, match="keyword_vocabulary"):
            CallsConfig(keyword_vocabulary=frozenset(refused))
