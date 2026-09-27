"""Quote-failure sub-codes (D3, A4's rest): each pass's refused answers are
counted by quote-check code on stage 1's and stage 2's outcome lines -- codes
and counts only, never a path, a quote or any word of the answer."""

from __future__ import annotations

import copy
import json
import logging

import httpx
import pytest

from dodeal_ai.units.call_intelligence.evidence import (
    SPEAKER_UNKNOWN,
    evidence_errors,
    quote_errors,
)
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.paid import QUOTE_FAILURES, PassUsage
from dodeal_ai.units.call_intelligence.stage2 import analyse_stage2
from dodeal_ai.units.call_intelligence.worker import process_call
from dodeal_ai.units.structured_intelligence.llm_call import (
    output_rejected,
    refused_codes,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit import test_call_stage1 as stage1_case
from tests.unit import test_call_stage2 as stage2_case
from tests.unit.test_call_passes import _call

ctx = stage2_case.ctx

INVENTED = "we sign the contract today"


def _line(caplog: pytest.LogCaptureFixture, message: str) -> logging.LogRecord:
    (line,) = [r for r in caplog.records if r.getMessage() == message]
    return line


def test_every_code_the_quote_check_gives_is_counted() -> None:
    """The vocabulary is the quote check's own: one failure of each kind."""
    call = _call()
    found = [
        *quote_errors(call, "w", None, "s1"),
        *quote_errors(call, "w", "the villa", "s9"),
        *quote_errors(call, "w", "word " * 41, "s1"),
        *quote_errors(call, "w", INVENTED, "s1"),
        *quote_errors(call, "w", "the sales office", "s1", speaker="client"),
        *evidence_errors(call, "w", None, None),
    ]
    assert {code for _, code in found} | {SPEAKER_UNKNOWN} == QUOTE_FAILURES


def test_only_quote_codes_are_counted_and_only_while_collected() -> None:
    usage = PassUsage()
    # Outside any collector a refusal is built as before and nothing is kept.
    output_rejected("label", (("wanted", "quote_length"),))
    with refused_codes() as codes:
        output_rejected("label", (("wanted", "quote_length"), ("", "json_invalid")))
        output_rejected("label", (("agreed.0", "quote_length"),))
    usage.refused("extract", codes)
    assert codes == ["quote_length", "json_invalid", "quote_length"]
    assert usage.quote_failures == {"extract": {"quote_length": 2}}


async def _stage1_line(
    llm: FakeLLM, caplog: pytest.LogCaptureFixture
) -> logging.LogRecord:
    """One stage-1 run of the pushed call with `llm`, and its outcome line."""
    transport = httpx.MockTransport(stage1_case._audio)
    async with httpx.AsyncClient(transport=transport) as http:
        run_ctx = {
            "http": http,
            "transcriber": FakeTranscriber(stage1_case.SEGMENTS),
            "resolve": stage1_case._resolve,
            "llm": llm,
        }
        await stage1_case._push()
        with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
            await process_call(run_ctx, "tenant-a", stage1_case.JOB)
    return _line(caplog, "call_job_outcome")


async def test_stage1s_line_counts_the_refused_answers_quote_codes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard: a broken shape with two failed quotes is refused once and
    answered well; the line counts the two quote codes, and no word said."""
    invented = copy.deepcopy(stage1_case.EXTRACTION)
    invented["details"]["budget"]["value"] = None
    invented["agreed"][0]["quote"] = INVENTED
    invented["wanted"]["segment"] = "s9"
    llm = FakeLLM(
        json_response(invented),
        json_response(stage1_case.EXTRACTION),
        json_response(stage1_case.PROSE),
    )

    line = await _stage1_line(llm, caplog)

    assert line.quote_failures == {
        "extract": {"segment_unknown": 1, "quote_not_in_segment": 1}
    }
    assert line.pass_tokens["extract"]["calls"] == 2
    written = json.dumps(line.__dict__, default=str)
    assert INVENTED not in written and "agreed" not in written


async def test_a_clean_stage1_run_has_no_quote_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    line = await _stage1_line(stage1_case._good_llm(), caplog)
    assert line.quote_failures is None


async def test_stage2s_line_counts_both_refused_answers_of_a_failed_pass(
    ctx: dict, caplog: pytest.LogCaptureFixture
) -> None:
    """An objection quoted from a segment that never said it, twice: the pass
    fails, and both refusals are counted under objections."""
    await stage2_case._done_and_pending(ctx)
    objection = {
        "category": "price",
        "quote": INVENTED,
        "segment": "s2",
        "addressed": "no",
        "agent_quote": None,
        "agent_segment": None,
        "satisfied": "unclear",
        "satisfied_quote": None,
        "satisfied_segment": None,
    }
    answer = {"objections": [objection]}
    stage2_ctx = stage2_case._stage2_ctx(answer, answer)

    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await analyse_stage2(stage2_ctx, "tenant-a", stage2_case.JOB)

    line = _line(caplog, "call_stage2_outcome")
    assert line.part_reasons == {
        "objections": "objections_malformed_output",
        "score": "scoring_off",
    }
    assert line.quote_failures == {"objections": {"quote_not_in_segment": 2}}
    assert INVENTED not in json.dumps(line.__dict__, default=str)
