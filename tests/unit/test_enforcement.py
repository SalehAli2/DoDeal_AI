"""Register item 142: the enforcement block, on every judgement.

The block exists so the CRM never infers. Two claims are tested separately
because they fail separately:

  THE VERDICT IS DERIVED, from the tenant's mode and what we already decided
  about the note -- never from a model, never from the request. The table below
  is every mode against every action AND against every suppression, because
  "always present" is only true if the suppressed paths carry one too.

  STRICT DOES NOT BLOCK TODAY. Blocking is scoped to a stage change and the
  request carries no field saying a lead is moving stage. So `block` is in the
  vocabulary and is never returned; a test asserts that across the whole cross
  product rather than at one point, since the day someone wires blocking up
  early is the day a tenant that merely SET strict starts refusing saves.
"""

from __future__ import annotations

import dataclasses

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.cost import limiter
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.config import (
    EnforcementMode,
    TenantConfig,
    get_tenant_config,
    parse_unit_a_section,
)
from dodeal_ai.units.structured_intelligence.decide import enforcement
from dodeal_ai.units.structured_intelligence.pipeline import (
    JudgementDeps,
    judge_note,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    DecisionAction,
    EnforcementTarget,
    EnforcementVerdict,
    JudgementRequest,
    SuppressedDetail,
)
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.fake_operational_redis import FakeOperationalRedis
from tests.helpers.score_answers import score_payload

CONFIG = get_tenant_config("tenant-a")

LEAD_ID = 1656
NOTE_ID = 10
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."
THIN_NOTE = "ok"


def _with_mode(mode: EnforcementMode) -> TenantConfig:
    """The default rubric with one field changed: only the mode varies, so a
    difference in the block below cannot come from a threshold."""
    return dataclasses.replace(CONFIG, enforcement_mode=mode)


# --- the table: every mode against every action -----------------------------


@pytest.mark.parametrize("mode", list(EnforcementMode))
def test_a_silently_accepted_note_is_allowed_in_every_mode(
    mode: EnforcementMode,
) -> None:
    """A note we would not ask about is not flagged, whatever the mode."""
    block = enforcement(_with_mode(mode), action=DecisionAction.ACCEPT_SILENT)
    assert block.verdict is EnforcementVerdict.ALLOW
    assert block.applies_to is None


@pytest.mark.parametrize(
    "action",
    [DecisionAction.ACCEPT_FLAG_PROMPT, DecisionAction.PROMPT_CLARIFICATION],
)
def test_off_allows_every_action(action: DecisionAction) -> None:
    """off is the whole point of off: the judgement is still made and still
    reported, and the CRM is told to do nothing about it."""
    block = enforcement(_with_mode(EnforcementMode.OFF), action=action)
    assert block.mode is EnforcementMode.OFF
    assert block.verdict is EnforcementVerdict.ALLOW
    assert block.applies_to is None


@pytest.mark.parametrize("mode", [EnforcementMode.ADVISORY, EnforcementMode.STRICT])
@pytest.mark.parametrize(
    "action",
    [DecisionAction.ACCEPT_FLAG_PROMPT, DecisionAction.PROMPT_CLARIFICATION],
)
def test_advisory_and_strict_flag_the_two_actions_that_say_something(
    mode: EnforcementMode, action: DecisionAction
) -> None:
    """Both actions that mean we have something to say flag the note, and
    strict answers identically to advisory -- that is today's rule, not an
    oversight."""
    block = enforcement(_with_mode(mode), action=action)
    assert block.mode is mode
    assert block.verdict is EnforcementVerdict.FLAG
    assert block.applies_to is EnforcementTarget.NOTE


# --- the table: every mode against every suppression ------------------------


@pytest.mark.parametrize("mode", [EnforcementMode.ADVISORY, EnforcementMode.STRICT])
def test_a_note_below_the_length_floor_is_flagged(mode: EnforcementMode) -> None:
    """The one suppression that is a complaint about the NOTE: too little was
    written, which is the salesperson's to fix."""
    block = enforcement(_with_mode(mode), detail=SuppressedDetail.NOTE_TOO_SHORT)
    assert block.verdict is EnforcementVerdict.FLAG
    assert block.applies_to is EnforcementTarget.NOTE


@pytest.mark.parametrize("mode", list(EnforcementMode))
@pytest.mark.parametrize(
    "detail",
    [
        SuppressedDetail.SYSTEM_EVENT,
        SuppressedDetail.UNCLASSIFIABLE,
        SuppressedDetail.NOTE_TOO_LONG,
    ],
)
def test_the_other_suppressions_are_allowed(
    mode: EnforcementMode, detail: SuppressedDetail
) -> None:
    """A machine timeline entry, a note the classifier could not place, and a
    note too long to be one interaction are all statements about what WE can
    judge. Flagging a salesperson for our own limits would be wrong."""
    block = enforcement(_with_mode(mode), detail=detail)
    assert block.verdict is EnforcementVerdict.ALLOW
    assert block.applies_to is None


def test_a_note_below_the_floor_is_allowed_when_enforcement_is_off() -> None:
    """off short-circuits before the detail is looked at."""
    block = enforcement(
        _with_mode(EnforcementMode.OFF), detail=SuppressedDetail.NOTE_TOO_SHORT
    )
    assert block.verdict is EnforcementVerdict.ALLOW
    assert block.applies_to is None


# --- block is never returned today ------------------------------------------


@pytest.mark.parametrize("mode", list(EnforcementMode))
def test_no_mode_returns_block_for_any_action_or_suppression(
    mode: EnforcementMode,
) -> None:
    """The whole cross product, plus the shape with neither input. Blocking is
    scoped to a stage change and the request carries no field for it, so a
    tenant that merely SET strict must not start refusing saves."""
    config = _with_mode(mode)
    blocks = [enforcement(config)]
    blocks += [enforcement(config, action=action) for action in DecisionAction]
    blocks += [enforcement(config, detail=detail) for detail in SuppressedDetail]

    assert all(x.verdict is not EnforcementVerdict.BLOCK for x in blocks)
    # applies_to is null exactly when nothing is flagged -- the invariant the
    # CRM reads, asserted over the same cross product rather than case by case.
    assert all(
        (x.applies_to is None) == (x.verdict is EnforcementVerdict.ALLOW)
        for x in blocks
    )


def test_block_is_in_the_vocabulary() -> None:
    """Built and recorded, never enabled: the member has to exist for the
    stage-change work to fill in, and its absence would be the easier bug."""
    assert EnforcementVerdict.BLOCK.value == "block"
    assert [m.value for m in EnforcementMode] == ["off", "advisory", "strict"]


# --- the tenant's mode, from a tenant's file --------------------------------


def test_a_tenant_file_setting_off_parses_and_is_carried() -> None:
    """The new vocabulary through the real parse path, not a hand-built enum."""
    config = parse_unit_a_section(
        {"config_version": "tenant-b-cfg-1", "enforcement_mode": "off"}
    )
    assert config.enforcement_mode is EnforcementMode.OFF
    assert enforcement(config, action=DecisionAction.PROMPT_CLARIFICATION).mode is (
        EnforcementMode.OFF
    )


def test_a_tenant_file_setting_strict_parses() -> None:
    config = parse_unit_a_section(
        {"config_version": "tenant-b-cfg-2", "enforcement_mode": "strict"}
    )
    assert config.enforcement_mode is EnforcementMode.STRICT


def test_the_old_blocking_spelling_no_longer_parses() -> None:
    """The reason config_version moved: a file written against the old
    vocabulary refuses startup rather than being read as something else."""
    with pytest.raises(ValueError):
        parse_unit_a_section(
            {"config_version": "tenant-b-cfg-3", "enforcement_mode": "blocking"}
        )


def test_the_default_tenant_launches_advisory() -> None:
    assert CONFIG.enforcement_mode is EnforcementMode.ADVISORY


# --- through the pipeline: present on both shapes ---------------------------


@pytest.fixture(autouse=True)
def cost(monkeypatch) -> FakeCostRedis:
    """db1, read by the token pre-flight on every judgement."""
    client = FakeCostRedis()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: client)
    return client


@pytest.fixture
def operational(monkeypatch) -> FakeOperationalRedis:
    client = FakeOperationalRedis()
    monkeypatch.setattr(state, "get_operational_client", lambda: client)
    return client


def _scope():
    return RequestContext(
        tenant="tenant-a",
        subject="42",
        database="crm_tenant_a",
        roles=(),
        permissions=frozenset(),
        request_id="req-1",
    ).scope()


def _deps(note_text: str, config: TenantConfig, *answers) -> JudgementDeps:
    return JudgementDeps(
        leads=FakeLeadsClient(
            leads={LEAD_ID: lead(LEAD_ID)},
            notes={LEAD_ID: [note(NOTE_ID, note_text)]},
        ),
        llm=FakeLLM(*answers),
        config=config,
        settings=get_settings(),
    )


def _happy_path():
    """The three scripted answers a scored judgement needs. The default checks
    give 55 of 80 -- `fair`, one below the accept threshold -- so the scored
    path lands on accept_flag_prompt."""
    return [
        json_response({"note_type": "discovery"}),
        json_response(
            {
                "is_vague": True,
                "missing_components": ["next_step_with_date"],
                "clarification_prompt": "Which Tuesday, and what will you cover?",
                "reasoning": "The follow-up has no date.",
            }
        ),
        json_response(score_payload()),
    ]


async def test_a_scored_judgement_carries_the_block(operational) -> None:
    deps = _deps(GOOD_NOTE, CONFIG, *_happy_path())

    judgement = await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=NOTE_ID),
        resubmission=False,
        deps=deps,
    )

    assert judgement.decision is not None
    assert judgement.decision.action is DecisionAction.ACCEPT_FLAG_PROMPT
    assert judgement.enforcement.mode is EnforcementMode.ADVISORY
    assert judgement.enforcement.verdict is EnforcementVerdict.FLAG
    assert judgement.enforcement.applies_to is EnforcementTarget.NOTE


async def test_a_suppressed_judgement_carries_the_block(operational) -> None:
    """The claim the whole piece rests on: no score, no decision, and still a
    verdict. Before this, the CRM had an absent action to infer from."""
    deps = _deps(THIN_NOTE, CONFIG)

    judgement = await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=NOTE_ID),
        resubmission=False,
        deps=deps,
    )

    assert judgement.score is None and judgement.decision is None
    assert judgement.suppressed is not None
    assert judgement.suppressed.detail_code is SuppressedDetail.NOTE_TOO_SHORT
    assert judgement.enforcement.verdict is EnforcementVerdict.FLAG
    assert judgement.enforcement.applies_to is EnforcementTarget.NOTE
    # Nothing was spent to reach it: the length gate runs before the first call.
    assert deps.llm.call_count == 0


async def test_the_mode_on_the_judgement_is_the_tenants_not_a_default(
    operational,
) -> None:
    """A tenant running `off` gets `off` stamped on the judgement, and the
    verdict that goes with it -- on the same note that flags under the
    default."""
    deps = _deps(GOOD_NOTE, _with_mode(EnforcementMode.OFF), *_happy_path())

    judgement = await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=NOTE_ID),
        resubmission=False,
        deps=deps,
    )

    assert judgement.decision is not None
    assert judgement.decision.action is DecisionAction.ACCEPT_FLAG_PROMPT
    assert judgement.enforcement.mode is EnforcementMode.OFF
    assert judgement.enforcement.verdict is EnforcementVerdict.ALLOW
    assert judgement.enforcement.applies_to is None


async def test_a_suppressed_judgement_under_off_is_allowed(operational) -> None:
    deps = _deps(THIN_NOTE, _with_mode(EnforcementMode.OFF))

    judgement = await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=NOTE_ID),
        resubmission=False,
        deps=deps,
    )

    assert judgement.suppressed is not None
    assert judgement.enforcement.mode is EnforcementMode.OFF
    assert judgement.enforcement.verdict is EnforcementVerdict.ALLOW


async def test_a_classifier_suppression_carries_the_block(operational) -> None:
    """A machine timeline entry: suppressed by a model call, allowed, and the
    mode still stamped."""
    deps = _deps(GOOD_NOTE, CONFIG, json_response({"note_type": "system_event"}))

    judgement = await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=NOTE_ID),
        resubmission=False,
        deps=deps,
    )

    assert judgement.suppressed is not None
    assert judgement.suppressed.detail_code is SuppressedDetail.SYSTEM_EVENT
    assert judgement.enforcement.mode is EnforcementMode.ADVISORY
    assert judgement.enforcement.verdict is EnforcementVerdict.ALLOW
    assert judgement.enforcement.applies_to is None


async def test_the_block_is_in_the_serialised_judgement(operational) -> None:
    """What the CRM actually receives: three named fields, always present."""
    deps = _deps(GOOD_NOTE, CONFIG, *_happy_path())

    judgement = await judge_note(
        _scope(),
        JudgementRequest(lead_id=LEAD_ID, note_id=NOTE_ID),
        resubmission=False,
        deps=deps,
    )

    assert judgement.model_dump(mode="json")["enforcement"] == {
        "mode": "advisory",
        "verdict": "flag",
        "applies_to": "note",
    }
