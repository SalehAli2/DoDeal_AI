"""Each fixed list code accepts is the list its prompt gives the model: a
value code knows and the prompt never names is one the model never answers."""

from __future__ import annotations

from typing import get_args

import pytest

from dodeal_ai.core.prompting import build_prompt
from dodeal_ai.units.call_intelligence.escalations import Issue
from dodeal_ai.units.call_intelligence.extras import MAX_KEYWORD_WORDS, Keyword
from dodeal_ai.units.call_intelligence.passes import (
    LOSS_CATEGORIES,
    NextKind,
    StatusDetail,
)
from dodeal_ai.units.call_intelligence.prompts import (
    ESCALATIONS_TEMPLATE,
    EXTRACT_TEMPLATE,
    EXTRAS_TEMPLATE,
)


def _text(template: str) -> str:
    return build_prompt(template, caller_data="").stable


def _values(annotation: object) -> tuple[str, ...]:
    """A Literal's values, through a type alias and an optional."""
    alias = getattr(annotation, "__value__", annotation)
    found: list[str] = []
    for arg in get_args(alias):
        found += [arg] if isinstance(arg, str) else list(_values(arg))
    return tuple(found)


@pytest.mark.parametrize(
    ("template", "values"),
    [
        (EXTRACT_TEMPLATE, _values(NextKind)),
        (EXTRACT_TEMPLATE, LOSS_CATEGORIES),
        (EXTRACT_TEMPLATE, _values(StatusDetail.model_fields["value"].annotation)),
        (EXTRAS_TEMPLATE, _values(Keyword.model_fields["kind"].annotation)),
        (ESCALATIONS_TEMPLATE, _values(Issue)),
    ],
    ids=["next-step-kind", "loss-reason", "property-status", "keyword-kind", "issue"],
)
def test_every_value_code_accepts_is_named_in_its_prompt(
    template: str, values: tuple[str, ...]
) -> None:
    assert values
    text = _text(template)
    assert [value for value in values if value not in text] == []


def test_the_extras_prompt_holds_said_to_a_name_of_five_words() -> None:
    assert MAX_KEYWORD_WORDS == 5
    assert "said is a NAME ONLY, at most five words" in _text(EXTRAS_TEMPLATE)


def test_the_extract_prompt_names_the_time_lines_it_is_given() -> None:
    text = _text(EXTRACT_TEMPLATE)
    assert "RECORDED AT" in text and "TIMEZONE" in text
    assert "2026-09-27T17:00:00+04:00" in text
