"""Prose numbers (A7): every number the summary or the CRM note writes in
digits must be one the masked transcript the prose pass read shows; else the
answer is malformed, reprompted once with the numbers tail, then prose_failed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.evidence import numbers_in
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.passes import (
    Extraction,
    Prose,
    check_prose,
    settled,
    write_prose,
)
from dodeal_ai.units.call_intelligence.prompts import NUMBERS_TAIL_TEMPLATE
from dodeal_ai.units.call_intelligence.worker import process_call
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.reprompt_tails import tail_with
from tests.unit.test_call_passes import SCOPE, _call, _extraction
from tests.unit.test_call_stage1 import (
    EXTRACTION,
    JOB,
    SEGMENTS,
    _audio,
    _Deliveries,
    _push,
    _resolve,
    _result,
)


def _prose(summary: str, crm_note: str = "Villa, next step a viewing.") -> Prose:
    return Prose(summary=summary, crm_note=crm_note)


def _refused(answer: Prose) -> tuple[tuple[str, str], ...]:
    with pytest.raises(OutputValidationError) as refused:
        check_prose(_call())(answer)
    return refused.value.errors


def test_a_number_is_read_in_any_digits_and_separators() -> None:
    assert numbers_in("1,200,000 AED, ١٬٢٠٠٬٠٠٠ and 1200000") == {"1200000"}
    assert numbers_in("at 16:30 on day ۷") == {"16", "30", "7"}
    assert numbers_in("no digits here") == set()


@pytest.mark.parametrize(
    "summary",
    [
        "The client's budget is 1,200,000 AED.",
        "The client's budget is 1200000 AED.",
        "The client's budget is ١٬٢٠٠٬٠٠٠ AED.",
        "The client wants a villa and a viewing on Tuesday at four.",
    ],
)
def test_a_number_the_transcript_shows_passes(summary: str) -> None:
    check_prose(_call())(_prose(summary))


def test_a_number_the_transcript_does_not_show_is_malformed() -> None:
    """The guard (A7): an invented amount, a worked-out date, and a phone
    number the pass saw only masked are each refused, field by field."""
    assert _refused(_prose("The budget is 1,300,000 AED.")) == (
        ("summary", "number_not_in_transcript"),
    )
    assert _refused(_prose("A viewing on 2026-09-29.", "Call 050 123 4567.")) == (
        ("summary", "number_not_in_transcript"),
        ("crm_note", "number_not_in_transcript"),
    )


async def test_one_reprompt_carries_the_numbers_tail() -> None:
    call = _call()
    kept = settled(Extraction.model_validate(_extraction()), call)
    bad = {"summary": "The budget is 1,300,000 AED.", "crm_note": "Villa."}
    good = {"summary": "The budget is 1,200,000 AED.", "crm_note": "Villa."}
    llm = FakeLLM(json_response(bad), json_response(good))

    answer, _ = await write_prose(llm, call, kept, scope=SCOPE, settings=get_settings())

    assert answer.summary == good["summary"] and llm.call_count == 2
    tail = tail_with(NUMBERS_TAIL_TEMPLATE, "$.summary: number_not_in_transcript")
    assert llm.calls[1].prompt.tail == tail


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    invented = json_response(
        {"summary": "The client wants a villa for 9,999,999 AED.", "crm_note": "Villa."}
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_audio)) as http:
        yield {
            "http": http,
            "transcriber": FakeTranscriber(SEGMENTS),
            "resolve": _resolve,
            "deliver": _Deliveries(),
            "llm": FakeLLM(json_response(EXTRACTION), invented, invented),
        }


async def test_an_invented_number_twice_is_prose_failed(ctx: dict[str, Any]) -> None:
    """A7 then A3: the prose refused twice, the analysis goes out without it."""
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    result = await _result()
    assert result["analysis_reason"] == "prose_failed"
    assert (result["analysis"]["summary"], result["analysis"]["crm_note"]) == (
        None,
        None,
    )
    assert ctx["llm"].call_count == 3
