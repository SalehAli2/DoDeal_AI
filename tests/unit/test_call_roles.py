"""unit_b.roles (roles.py): which diarized voice is the agent, quoted from its
own segment, and code's word on whether the mapping is applied."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.language import UNHEARD, Spoken
from dodeal_ai.units.call_intelligence.roles import (
    Roles,
    apply_roles,
    check_roles,
    judge,
    opening,
    spoken,
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


def _applied(transcript: Transcript, answer: Roles, seconds: float = 150):
    """The answer judged against the view the pass was shown, then applied."""
    view = opening(transcript, country_code="971")
    return apply_roles(transcript, judge(view, answer), call_seconds=seconds)


def test_this_is_nada_from_the_company_maps_that_voice_to_agent() -> None:
    """The quoted introduction makes speaker_2 the agent; the other, the client."""
    answer = _answer(
        _role("speaker_1", "client", "I want a villa near the sea", "s3"),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
    )
    check_roles(opening(OPENING, country_code="971"))(answer)
    relabelled, block = _applied(OPENING, answer)
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
def test_an_invented_or_borrowed_quote_is_not_verified(agent: dict, error: str) -> None:
    """The guard (A1): a quote verifies a voice only in a segment with that
    voice's own label. Beside a verified client it is inferred the agent,
    unverified; with no role verified the answer is malformed."""
    call = opening(OPENING, country_code="971")
    answer = _answer(_role("speaker_1", "client", "I want a villa", "s3"), agent)
    check_roles(call)(answer)
    judged = judge(call, answer)
    assert judged.verified == (True, False)
    assert judged.speakers[1].model_dump() == _role("speaker_2", "agent", None, None)
    guessed = _answer(_role("speaker_1", "client", "I want a house", "s3"), agent)
    with pytest.raises(OutputValidationError) as refused:
        check_roles(call)(guessed)
    assert refused.value.errors == (
        ("speakers.0", "quote_not_in_segment"),
        ("speakers.1", error),
        ("speakers", "no_role_verified"),
    )


def test_a_voices_quote_is_found_in_any_of_its_own_segments() -> None:
    """Cited at the other voice's segment, the client's words are found in
    its own s3, stored there, and verify it."""
    answer = _answer(
        _role("speaker_1", "client", "I want a villa near the sea", "s2"),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
    )
    judged = judge(opening(OPENING, country_code="971"), answer)
    assert judged.verified == (True, True) and judged.errors == ()
    assert [s.segment for s in judged.speakers] == ["s3", "s2"]


def test_two_voices_with_one_verified_role_map_the_other_to_the_other_role() -> None:
    """A1: the agent verified and the other voice unclear, the other is the
    client; the mapping is applied and the block says which was verified."""
    answer = _answer(
        _role("speaker_1", "unclear", None, None),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
    )
    check_roles(opening(OPENING, country_code="971"))(answer)
    relabelled, block = _applied(OPENING, answer)
    assert [s.speaker for s in relabelled.segments] == ["client", "agent", "client"]
    assert (block["applied"], block["reasons"]) == (True, [])
    assert block["speakers"] == [
        {**_role("speaker_1", "client", None, None), "verified": False},
        {**answer.speakers[1].model_dump(), "verified": True},
    ]


def test_three_voices_never_infer_a_role() -> None:
    """The guard (A1): inference is for two voices only; on three, every
    agent or client quote must verify, as before."""
    crowded = Transcript.of(
        (*OPENING.segments, _say(20, "speaker_3", "Hello, I am the brother.")),
        provider="recorded",
        model="recorded-stt-1",
    )
    answer = _answer(
        _role("speaker_1", "client", "I want a house", "s3"),
        _role("speaker_2", "agent", "this is Nada from the company", "s2"),
        _role("speaker_3", "client", "I am the brother", "s4"),
    )
    with pytest.raises(OutputValidationError) as refused:
        check_roles(opening(crowded, country_code="971"))(answer)
    assert refused.value.errors == (("speakers.0", "quote_not_in_segment"),)
    _, block = _applied(crowded, answer)
    assert block["applied"] is False
    assert block["reasons"] == ["speakers_over_two", "roles_unclear"]


def test_a_failed_language_quote_keeps_the_mapping_and_falls_back_to_script() -> None:
    """The guard (A1): the languages are judged apart. The client's language
    quoted from the agent's voice is dropped, never a malformed answer; the
    mapping is applied and the call's languages come from the script."""
    answer = Roles.model_validate(
        {
            "speakers": [
                _role("speaker_1", "client", "I want a villa near the sea", "s3"),
                _role("speaker_2", "agent", "this is Nada from the company", "s2"),
            ],
            "call_languages": {
                "client": {"language": "en", "quote": "this is Nada", "segment": "s2"},
                "agent": {"language": "en", "quote": "this is Nada", "segment": "s2"},
            },
        }
    )
    view = opening(OPENING, country_code="971")
    check_roles(view)(answer)
    judged = judge(view, answer)
    assert judged.languages_failed is True
    _, block = apply_roles(OPENING, judged, call_seconds=150)
    assert block["applied"] is True
    assert block["call_languages"] == {
        "client": {"language": None, "quote": None, "segment": None},
        "agent": {"language": "en", "quote": "this is Nada", "segment": "s2"},
    }
    assert spoken(judged, applied=True) == UNHEARD
    kept = answer.model_copy(
        update={"call_languages": answer.call_languages.model_copy(
            update={"client": answer.call_languages.client.model_copy(
                update={"quote": "I want a villa", "segment": "s3"})})}
    )  # fmt: skip
    assert spoken(judge(view, kept), applied=True) == Spoken(client="en", agent="en")


def test_an_unclear_failed_or_crowded_mapping_makes_the_transcript_uncertain() -> None:
    both_unclear = _answer(
        _role("speaker_1", "unclear", None, None),
        _role("speaker_2", "unclear", None, None),
    )
    call = opening(OPENING, country_code="971")
    with pytest.raises(OutputValidationError) as refused:
        check_roles(call)(both_unclear)
    assert refused.value.errors == (("speakers", "no_role_verified"),)
    with pytest.raises(OutputValidationError) as refused:
        check_roles(call)(_answer(_role("speaker_2", "agent", "this is Nada", "s2")))
    assert refused.value.errors == (("speakers", "not_each_once"),)
    kept, block = apply_roles(OPENING, None, call_seconds=150)
    assert kept.segments == OPENING.segments and kept.uncertain
    assert (block["applied"], block["reasons"]) == (False, ["roles_failed"])
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
    relabelled, block = _applied(crowded, mapped)
    assert relabelled.speakers == ("client", "agent") and relabelled.uncertain
    assert (block["applied"], block["reasons"]) == (True, ["speakers_over_two"])
    unclear = _answer(*mapped.speakers[:2], _role("speaker_3", "unclear", None, None))
    check_roles(opening(crowded, country_code="971"))(unclear)
    kept, block = _applied(crowded, unclear)
    assert kept.segments == crowded.segments and kept.uncertain
    assert block["reasons"] == ["speakers_over_two", "roles_unclear"]


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
    kept, block = _applied(ALONE, agent, seconds)
    assert kept.segments == ALONE.segments and kept.speakers == ("speaker_1",)
    assert kept.uncertain and list(kept.uncertain_reasons) == reasons
    assert (block["applied"], block["reasons"]) == (False, reasons)


def test_two_agents_or_two_clients_verified_is_malformed() -> None:
    """Two voices both verified alike: no mapping, and malformed."""
    for first, second in (("agent", "agent"), ("client", "client")):
        answer = _answer(
            _role("speaker_1", first, "Hello, who is calling please?", "s1"),
            _role("speaker_2", second, "this is Nada from the company", "s2"),
        )
        with pytest.raises(OutputValidationError) as refused:
            check_roles(opening(OPENING, country_code="971"))(answer)
        assert refused.value.errors == (("speakers", "same_role"),)
        kept, block = _applied(OPENING, answer)
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
    """The guard (F-3): no role verified twice, or no answer, so the engine's
    labels stay; the agent's alarm phrase and number are put down to no one,
    and each is an off_channel_contact_review, never the client's."""
    unclear = json_response(
        {
            "speakers": [
                _role("speaker_1", "unclear", None, None),
                _role("speaker_2", "unclear", None, None),
            ]
        }
    )
    ctx["llm"] = (
        FakeLLM(unclear, unclear, json_response(EXTRACTION), json_response(PROSE))
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
