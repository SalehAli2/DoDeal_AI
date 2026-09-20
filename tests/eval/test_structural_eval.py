"""Structural eval: every note in the corpus comes out as a judgement or a
suppression, and never as an exception.

WHAT THIS IS NOT. It says nothing about whether a judgement is any GOOD. The
model is `FakeLLM` scripted to one valid answer per template, so every note gets
the same marks and the same classification; what varies is the note, and what is
being tested is the pipeline's behaviour across 127 real-shaped notes rather
than across the two or three a unit test writes for itself. Quality needs a real
model and lives in `test_quality_eval.py`, skipped until step 18.

WHY IT RUNS IN THE DEFAULT SUITE. It is hermetic, it makes no network call and
it takes under a second. A corpus note that crashes the pipeline -- an encoding
the prompt builder mishandles, a length that trips a cap, an empty author -- is
a bug that should break CI, not one that waits for someone to run an optional
marker.

THE TWO HALVES. The 127 `notes` are salesperson-written and go through the whole
pipeline. The 27 `timeline_events` are machine-written and must stop after
classification: the point of the `system_event` short-circuit is that it costs
nothing, so the second half asserts no vague and no score call was ever issued
for any of them.
"""

from __future__ import annotations

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.cost import limiter
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.pipeline import (
    JudgementDeps,
    _is_recognised_short_note,
    judge_note,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    JudgementRequest,
    NoteType,
    SuppressedDetail,
    SuppressedReason,
)
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_leads import (
    FakeLeadsClient,
    load_fixture_client,
    load_fixture_timeline_events,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.fake_operational_redis import FakeOperationalRedis

pytestmark = pytest.mark.eval

CONFIG = get_tenant_config("tenant-a")

# Pinned in tests/unit/test_fake_crm_fixture.py; restated here because the two
# numbers below are what make this an eval over the WHOLE corpus rather than
# over whatever the loader happened to return.
NOTE_COUNT = 127
TIMELINE_COUNT = 27
# min_note_chars 15 / min_note_tokens 3, applied to the stripped text. Counted
# from the fixture, not predicted. If a regenerated corpus changes it, this
# fails and says by how much -- which is the point of pinning it.
#
# Nine notes are below the floor, and one of them ("not interested") is a
# recognised outcome (register item 132), so it is judged like any other note
# and is not counted here: eight are suppressed with the fixed question.
NOTE_TOO_SHORT_COUNT = 8
# max_note_chars 2000, the other end of the same gate (Piece K). Three corpus
# notes are over 5,000 characters and every other note is under 400, so this
# count is not sensitive to where between 400 and 5,000 the limit is set -- it
# would take a real change in the corpus to move it, which is what makes it
# worth pinning. None of the three is also thin.
NOTE_TOO_LONG_COUNT = 3
SCORED_COUNT = NOTE_COUNT - NOTE_TOO_SHORT_COUNT - NOTE_TOO_LONG_COUNT

VAGUE_ANSWER = {
    "is_vague": True,
    "missing_components": ["next_step_with_date"],
    "clarification_prompt": "When are you following up with this client?",
    "reasoning": "No date was given for the next step.",
}
SCORE_ANSWER = {
    "marks": {
        "what_happened": 20,
        "client_said": 15,
        "next_step_date": 15,
        "clarity": 5,
    }
}
# Every note is classified `discovery`. Not a claim about the corpus -- it is
# the type whose rubric leaves all four unsuppressed components in play, so it
# exercises the most of the scoring path per note.
CLASSIFY_AS = NoteType.DISCOVERY


def _scope():
    return RequestContext(
        tenant="tenant-a",
        subject="42",
        database="crm_tenant_a",
        roles=(),
        permissions=frozenset(),
        request_id="req-eval",
    ).scope()


@pytest.fixture(autouse=True)
def cost(monkeypatch) -> FakeCostRedis:
    """db1, faked for the whole corpus run: the token pre-flight reads it once
    per note, and 127 notes must not become 127 socket attempts."""
    client = FakeCostRedis()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: client)
    return client


@pytest.fixture
def operational(monkeypatch) -> FakeOperationalRedis:
    client = FakeOperationalRedis()
    monkeypatch.setattr(state, "get_operational_client", lambda: client)
    return client


def _corpus_client() -> FakeLeadsClient:
    return load_fixture_client()


async def test_every_corpus_note_yields_a_judgement_or_a_suppression(operational):
    client = _corpus_client()
    every_note = [(lid, n) for lid, notes in client.notes.items() for n in notes]
    assert len(every_note) == NOTE_COUNT

    llm = FakeLLM()
    # One answer per template per note, queued ahead. Thin notes never reach the
    # model, so the queues end with items left over -- which is fine, and is why
    # this queues NOTE_COUNT rather than asserting the queues drain.
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *[json_response({"note_type": CLASSIFY_AS.value})] * NOTE_COUNT,
    )
    llm.script_for(
        template_for(CLASSIFY_AS), *[json_response(VAGUE_ANSWER)] * NOTE_COUNT
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(SCORE_ANSWER)] * NOTE_COUNT)

    deps = JudgementDeps(leads=client, llm=llm, config=CONFIG, settings=get_settings())

    scored = 0
    suppressed_short = 0
    suppressed_long = 0
    for lead_id, note in every_note:
        judgement = await judge_note(
            _scope(),
            JudgementRequest(lead_id=lead_id, note_id=note.id),
            resubmission=False,
            deps=deps,
        )

        # Exactly one of the two shapes, never a mixture: a judgement carrying
        # both a score and a suppression would mean the pipeline stopped and
        # carried on at the same time.
        if judgement.suppressed is None:
            assert judgement.score is not None, note.id
            assert judgement.decision is not None, note.id
            scored += 1
        else:
            assert judgement.score is None, note.id
            assert judgement.decision is None, note.id
            if judgement.suppressed.reason is SuppressedReason.INSUFFICIENT_EVIDENCE:
                assert (
                    judgement.suppressed.detail_code is SuppressedDetail.NOTE_TOO_SHORT
                ), note.id
                suppressed_short += 1
            elif judgement.suppressed.detail_code is SuppressedDetail.NOTE_TOO_LONG:
                # not_scorable, and reached before any model call -- so it is
                # stamped with no model, exactly as a thin note is.
                assert judgement.suppressed.reason is SuppressedReason.NOT_SCORABLE
                assert judgement.versions.model_version == "", note.id
                suppressed_long += 1

        # Present on every outcome, both shapes: a judgement nobody can trace
        # back to a rubric and a config version is not auditable.
        assert judgement.versions is not None, note.id
        assert judgement.note_id == note.id

    assert suppressed_short == NOTE_TOO_SHORT_COUNT
    assert suppressed_long == NOTE_TOO_LONG_COUNT
    assert scored == SCORED_COUNT
    assert scored + suppressed_short + suppressed_long == NOTE_COUNT


async def test_the_thin_notes_are_suppressed_before_any_model_call(operational):
    """The eight unrecognised short notes cost nothing: no reservation, no model call.

    Run on their own with an EMPTY model, so any call at all raises
    FakeLLMExhausted instead of quietly succeeding.
    """
    client = _corpus_client()
    thin = [
        (lid, n)
        for lid, notes in client.notes.items()
        for n in notes
        if (
            len(n.note.strip()) < CONFIG.min_note_chars
            or len(n.note.strip().split()) < CONFIG.min_note_tokens
        )
        and not _is_recognised_short_note(n.note.strip(), CONFIG)
    ]
    assert len(thin) == NOTE_TOO_SHORT_COUNT

    llm = FakeLLM()  # nothing scripted, positional or by template
    deps = JudgementDeps(leads=client, llm=llm, config=CONFIG, settings=get_settings())

    for lead_id, note in thin:
        judgement = await judge_note(
            _scope(),
            JudgementRequest(lead_id=lead_id, note_id=note.id),
            resubmission=False,
            deps=deps,
        )
        assert judgement.suppressed is not None
        assert judgement.suppressed.reason is SuppressedReason.INSUFFICIENT_EVIDENCE

    assert llm.call_count == 0
    # The fixed question (item 64) still takes attempt/rate slots, but no
    # judgement is ever reserved for a thin note.
    assert not any(key.startswith("idem:") for key in operational.store)


async def test_the_over_long_notes_are_suppressed_before_any_model_call(operational):
    """The other end of the same gate, and it costs the same nothing.

    Three corpus notes are over 5,000 characters -- pasted threads, not single
    interactions. They stop where a thin note stops: before the reservation and
    before the first paid call, so the salesperson can split the note and
    resubmit at once instead of meeting a 409 for the next 24 hours.
    """
    client = _corpus_client()
    over_long = [
        (lid, n)
        for lid, notes in client.notes.items()
        for n in notes
        if len(n.note.strip()) > CONFIG.max_note_chars
    ]
    assert len(over_long) == NOTE_TOO_LONG_COUNT

    llm = FakeLLM()  # nothing scripted: any model call at all raises
    deps = JudgementDeps(leads=client, llm=llm, config=CONFIG, settings=get_settings())

    for lead_id, note in over_long:
        judgement = await judge_note(
            _scope(),
            JudgementRequest(lead_id=lead_id, note_id=note.id),
            resubmission=False,
            deps=deps,
        )
        assert judgement.suppressed is not None
        assert judgement.suppressed.reason is SuppressedReason.NOT_SCORABLE
        assert judgement.suppressed.detail_code is SuppressedDetail.NOTE_TOO_LONG
        assert judgement.score is None and judgement.decision is None

    assert llm.call_count == 0
    assert operational.store == {}


async def test_every_timeline_event_is_suppressed_as_not_scorable(operational):
    """The machine-written half. Classification answers system_event and the
    pipeline stops there -- so the vague and score queues must be untouched."""
    events = load_fixture_timeline_events()
    flat = [(lid, e) for lid, events_ in events.items() for e in events_]
    assert len(flat) == TIMELINE_COUNT

    client = FakeLeadsClient(leads=load_fixture_client().leads, notes=events)
    llm = FakeLLM()
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *[json_response({"note_type": "system_event"})] * TIMELINE_COUNT,
    )
    # Deliberately queued and deliberately never popped. An empty template queue
    # raises rather than falling through (Piece I.2), so if the pipeline DID
    # issue one of these calls the assertion below would catch it -- and if it
    # issued more than TIMELINE_COUNT, FakeLLM would raise by name.
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        *[json_response(VAGUE_ANSWER)] * TIMELINE_COUNT,
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(SCORE_ANSWER)] * TIMELINE_COUNT)

    deps = JudgementDeps(leads=client, llm=llm, config=CONFIG, settings=get_settings())

    reached_model = 0
    for lead_id, event in flat:
        judgement = await judge_note(
            _scope(),
            JudgementRequest(lead_id=lead_id, note_id=event.id),
            resubmission=False,
            deps=deps,
        )
        assert judgement.suppressed is not None, event.id
        assert judgement.score is None and judgement.decision is None, event.id
        if judgement.suppressed.reason is SuppressedReason.NOT_SCORABLE:
            reached_model += 1
            assert judgement.suppressed.detail_code.value == "system_event", event.id

    # Every call that was made was a CLASSIFICATION call. This is the assertion
    # that "system_event costs one pass, not three" is worth: two more passes
    # per event is two more paid calls for text nobody wrote.
    classify_calls = [p for p in llm.prompts if "classify" in p.stable[:400].lower()]
    assert len(llm.prompts) == reached_model
    assert len(classify_calls) == len(llm.prompts)


async def test_the_corpus_runs_without_a_single_exception(operational):
    """The claim in one line, stated separately so a failure names it.

    Everything above asserts a shape. This asserts the thing the eval exists
    for: 127 notes through the real pipeline, no traceback.
    """
    client = _corpus_client()
    every_note = [(lid, n) for lid, notes in client.notes.items() for n in notes]

    llm = FakeLLM()
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *[json_response({"note_type": CLASSIFY_AS.value})] * NOTE_COUNT,
    )
    llm.script_for(
        template_for(CLASSIFY_AS), *[json_response(VAGUE_ANSWER)] * NOTE_COUNT
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(SCORE_ANSWER)] * NOTE_COUNT)
    deps = JudgementDeps(leads=client, llm=llm, config=CONFIG, settings=get_settings())

    failures: list[tuple[int, str]] = []
    for lead_id, note in every_note:
        try:
            await judge_note(
                _scope(),
                JudgementRequest(lead_id=lead_id, note_id=note.id),
                resubmission=False,
                deps=deps,
            )
        except Exception as exc:  # noqa: BLE001 - the whole point is to catch any
            failures.append((note.id, type(exc).__name__))

    assert failures == []
