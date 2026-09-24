"""unit_b.roles (roles.py): which diarized voice is the agent, quoted from its
own segment, and code's word on whether the mapping is applied."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.roles import (
    Roles,
    apply_roles,
    check_roles,
    opening,
)
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from dodeal_ai.units.call_intelligence.worker import process_call
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_stage1 import (
    EXTRACTION,
    JOB,
    PROSE,
    SEGMENTS,
    _audio,
    _Deliveries,
    _push,
    _resolve,
    _result,
    _say,
)

OPENING = Transcript.of(
    (
        _say(0, "speaker_1", "Hello, who is calling please?"),
        _say(5, "speaker_2", "Hi, this is Nada from the company, about your villa."),
        _say(10, "speaker_1", "Yes, I want a villa near the sea."),
    ),
    provider="recorded",
    model="recorded-stt-1",
)


def _role(speaker: str, role: str, quote: str | None, segment: str | None) -> dict:
    return {"speaker": speaker, "role": role, "quote": quote, "segment": segment}


def _answer(*speakers: dict) -> Roles:
    return Roles.model_validate({"speakers": list(speakers)})


def test_this_is_nada_from_the_company_maps_that_voice_to_agent() -> None:
    """The quoted introduction makes speaker_2 the agent; the other, the client."""
    answer = _answer(
        _role("speaker_1", "client", "I want a villa near the sea", "s3"),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
    )
    check_roles(opening(OPENING, country_code="971"))(answer)
    relabelled, block = apply_roles(OPENING, answer, call_seconds=150)
    assert [s.speaker for s in relabelled.segments] == ["client", "agent", "client"]
    assert relabelled.uncertain is False
    assert (block["applied"], block["reasons"]) == (True, [])


@pytest.mark.parametrize(
    ("agent", "error"),
    [
        (_role("speaker_2", "agent", "this is Nada from the bank", "s2"), "quote_not_in_segment"),
        (_role("speaker_2", "agent", "Hello, who is calling", "s1"), "quote_wrong_speaker"),
    ],
)  # fmt: skip
def test_an_invented_or_borrowed_quote_fails_the_check(agent: dict, error: str) -> None:
    answer = _answer(_role("speaker_1", "client", "I want a villa", "s3"), agent)
    with pytest.raises(OutputValidationError) as refused:
        check_roles(opening(OPENING, country_code="971"))(answer)
    assert refused.value.errors == (("speakers.1", error),)


def test_an_unclear_failed_or_crowded_mapping_makes_the_transcript_uncertain() -> None:
    unclear = _answer(
        _role("speaker_1", "unclear", None, None),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
    )
    call = opening(OPENING, country_code="971")
    check_roles(call)(unclear)
    with pytest.raises(OutputValidationError) as refused:
        check_roles(call)(_answer(unclear.speakers[1].model_dump()))
    assert refused.value.errors == (("speakers", "not_each_once"),)
    for answer, reasons in ((unclear, ["roles_unclear"]), (None, ["roles_failed"])):
        kept, block = apply_roles(OPENING, answer, call_seconds=150)
        assert kept.segments == OPENING.segments and kept.uncertain
        assert (block["applied"], block["reasons"]) == (False, reasons)
    crowded = Transcript.of(
        (*OPENING.segments, _say(20, "speaker_3", "Hello, I am the brother.")),
        provider="recorded",
        model="recorded-stt-1",
    )
    mapped = _answer(
        _role("speaker_1", "client", "I want a villa", "s3"),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
        _role("speaker_3", "client", "I am the brother", "s4"),
    )
    relabelled, block = apply_roles(crowded, mapped, call_seconds=150)
    assert relabelled.speakers == ("client", "agent") and relabelled.uncertain
    assert (block["applied"], block["reasons"]) == (True, ["speakers_over_two"])


ALONE = Transcript.of(
    (_say(0, "speaker_1", "Hi, this is Nada from the company, about your villa."),),
    provider="recorded",
    model="recorded-stt-1",
)


@pytest.mark.parametrize(
    ("seconds", "reasons"),
    [(30, ["single_voice", "roles_unclear"]), (29, ["roles_unclear"])],
)
def test_one_voice_called_agent_is_not_applied_and_is_uncertain(
    seconds: int, reasons: list[str]
) -> None:
    """The guard (F-2): an agent with no client is no mapping, and one voice
    on a call of 30 s or more is doubted as single_voice besides."""
    agent = _answer(_role("speaker_1", "agent", "this is Nada from the company", "s1"))
    check_roles(opening(ALONE, country_code="971"))(agent)
    kept, block = apply_roles(ALONE, agent, call_seconds=seconds)
    assert kept.segments == ALONE.segments and kept.speakers == ("speaker_1",)
    assert kept.uncertain and list(kept.uncertain_reasons) == reasons
    assert (block["applied"], block["reasons"]) == (False, reasons)


def test_two_agents_or_no_agent_is_no_mapping() -> None:
    """Exactly one agent and at least one client, or nothing is applied."""
    for first, second in (("agent", "agent"), ("client", "client")):
        answer = _answer(
            _role("speaker_1", first, "Hello, who is calling please?", "s1"),
            _role("speaker_2", second, "this is Nada from the company", "s2"),
        )
        kept, block = apply_roles(OPENING, answer, call_seconds=150)
        assert kept.speakers == ("speaker_1", "speaker_2") and kept.uncertain
        assert (block["applied"], block["reasons"]) == (False, ["roles_unclear"])


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    labels = {"agent": "speaker_1", "lead": "speaker_2"}
    diarized = tuple(
        s.model_copy(update={"speaker": labels[s.speaker]}) for s in SEGMENTS
    )
    roles = {
        "speakers": [
            _role("speaker_1", "agent", "this is the sales office", "s1"),
            _role("speaker_2", "client", "I want a villa", "s2"),
        ]
    }
    async with httpx.AsyncClient(transport=httpx.MockTransport(_audio)) as http:
        yield {
            "http": http,
            "transcriber": FakeTranscriber(diarized),
            "resolve": _resolve,
            "deliver": _Deliveries(),
            "llm": FakeLLM(
                json_response(roles), json_response(EXTRACTION), json_response(PROSE)
            ),
        }


async def test_stage_1_carries_the_roles_and_every_pass_reads_agent_and_client(
    ctx: dict[str, Any],
) -> None:
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    result = await _result()
    assert result["roles"]["applied"] is True
    speakers = [s["speaker"] for s in result["transcript"]["segments"]]
    assert speakers == ["agent", "client", "agent", "client"]
    assert result["analysis"] is not None and "roles" in result["versions"]["passes"]
    assert "[s1 00:00 speaker_1]" in ctx["llm"].calls[0].prompt.variable


@pytest.mark.parametrize(("duration", "doubted"), [(150, True), (29, False)])
async def test_one_voice_named_by_its_channel_is_doubted_on_a_long_call(
    ctx: dict[str, Any], duration: int, doubted: bool
) -> None:
    """No engine label, so no roles pass: a long call heard as one side alone
    is still single_voice."""
    alone = tuple(s.model_copy(update={"speaker": "agent"}) for s in SEGMENTS)
    ctx["transcriber"] = FakeTranscriber(alone)
    await _push(duration=duration, min_transcribe_seconds=10)
    await process_call(ctx, "tenant-a", JOB)
    result = await _result()
    assert result["roles"] is None
    reasons = result["transcript"]["uncertain_reasons"]
    assert reasons == (["single_voice"] if doubted else [])


@pytest.mark.parametrize("answered", [True, False])
async def test_unapplied_roles_and_an_alarm_phrase_give_a_review_escalation(
    ctx: dict[str, Any], answered: bool
) -> None:
    """The guard (F-3): an unclear answer or none, so the engine's labels stay;
    the agent's alarm phrase and number are put down to no one, and each is
    an off_channel_contact_review, never the client's."""
    unclear = {
        "speakers": [
            _role("speaker_1", "agent", "this is the sales office", "s1"),
            _role("speaker_2", "unclear", None, None),
        ]
    }
    ctx["llm"] = (
        FakeLLM(json_response(unclear), json_response(EXTRACTION), json_response(PROSE))
        if answered
        else None
    )
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    result = await _result()
    assert result["roles"]["applied"] is False
    signals = result["signals"]
    where = {"speaker": "unknown", "start_s": 10.0, "segment": "s3"}
    assert signals["alarms"] == [{"phrase": 0, **where}]
    assert signals["numbers"] == [
        {**where, "last4": "4321", "match": "unattributed_number"}
    ]
    assert signals["escalations"] == [
        {"type": "off_channel_contact_review", "source": "number", **where},
        {
            "type": "off_channel_contact_review",
            "source": "alarm_phrase",
            "phrase": 0,
            **where,
        },
    ]
