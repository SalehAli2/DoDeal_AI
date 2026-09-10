"""Unit A contracts: the vocabularies are complete, the request forbids extras,
the vagueness biconditional holds both ways, and no model-output schema can
carry a band or a total.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from dodeal_ai.units.structured_intelligence.schemas import (
    MAX_NOTE_TEXT_CHARS,
    UNCLASSIFIABLE,
    Band,
    ClassificationOutput,
    ComponentName,
    Decision,
    DecisionAction,
    DirectJudgementRequest,
    JudgementRequest,
    LeadContext,
    MissingComponent,
    NoteAnalysis,
    NoteType,
    PromptWithheld,
    ScoreOutput,
    Suppressed,
    SuppressedDetail,
    SuppressedReason,
    VagueOutput,
)

# The three schemas that describe UNTRUSTED model output. Kept as a tuple so a
# new output schema added without a band/total guard fails the loop below.
_OUTPUT_SCHEMAS = (ClassificationOutput, VagueOutput, ScoreOutput)

# The schemas a CALLER fills in. The direct route's body is here for the same
# reason the model's answers are: it is written by something outside this
# service, so it may not carry the answer either. LeadContext is included
# because it is the half of that body the classifier's prompt reads.
_REQUEST_SCHEMAS = (JudgementRequest, DirectJudgementRequest, LeadContext)

_SRC = Path(__file__).resolve().parents[2] / "src" / "dodeal_ai"

# The detail code Phase H deleted. Kept here as the ONE spelling in the repo,
# so the grep below is the thing that would have to be edited to let it back.
_DELETED_DETAIL_CODE = "not_" + "implemented"


# --- vocabularies ----------------------------------------------------------


def test_note_type_has_seven_members_including_system_event() -> None:
    assert [t.value for t in NoteType] == [
        "no_contact",
        "callback",
        "discovery",
        "viewing",
        "negotiation",
        "won_lost",
        "system_event",
    ]
    # ASSUMPTION[Q6]: the notes feed may carry backend timeline entries.
    assert NoteType.SYSTEM_EVENT in NoteType


def test_component_name_is_the_five_in_fixed_response_order() -> None:
    # Declaration order IS the order the response lists components in, so a
    # caller may index rather than match on name.
    assert [c.value for c in ComponentName] == [
        "what_happened",
        "client_said",
        "next_step_date",
        "deal_specifics",
        "clarity",
    ]


def test_missing_component_is_a_narrower_vocabulary_than_component_name() -> None:
    # Deliberately distinct enums: the date member is spelled differently and
    # only three of the five components can be asked about.
    assert [m.value for m in MissingComponent] == [
        "what_happened",
        "client_said",
        "next_step_with_date",
    ]
    assert MissingComponent.NEXT_STEP_WITH_DATE.value != ComponentName.NEXT_STEP_DATE


def test_remaining_vocabularies_are_closed_sets() -> None:
    assert [b.value for b in Band] == ["poor", "fair", "good", "excellent"]
    assert [a.value for a in DecisionAction] == [
        "accept_silent",
        "accept_flag_prompt",
        "prompt_clarification",
    ]
    assert [p.value for p in PromptWithheld] == [
        "resubmission",
        "attempt_cap",
        "rate_limited",
        "nothing_to_ask",
    ]
    assert [s.value for s in SuppressedReason] == [
        "insufficient_evidence",
        "not_scorable",
    ]
    # Four, and `not_implemented` is not one of them: it was deleted in Phase H
    # with the gap it named. The vocabulary says why a NOTE cannot be scored,
    # never why we have not finished building. `note_too_long` is the fourth,
    # added with the direct route and applying to both routes.
    assert [d.value for d in SuppressedDetail] == [
        "note_too_short",
        "system_event",
        "unclassifiable",
        "note_too_long",
    ]


def test_the_not_implemented_detail_code_is_gone_from_src() -> None:
    """The stub cannot outlive the work it stood in for.

    `not_implemented` meant "we have not built this yet", which to the CRM is
    indistinguishable from "this note cannot be scored" -- the same 200, the
    same suppressed shape, and no way to tell an unfinished service from an
    unscoreable note. Phase H deleted it with the decide() gap it named. This
    greps src/ rather than asserting on the enum, because the enum is only one
    of the places a string can come back.
    """
    offenders = []
    for path in sorted(_SRC.rglob("*.py")):
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _DELETED_DETAIL_CODE in line:
                offenders.append(f"{path.relative_to(_SRC).as_posix()}:{number}")

    assert offenders == [], (
        f"{_DELETED_DETAIL_CODE!r} was deleted in Phase H with the gap it named; "
        f"found again in: {offenders}"
    )


# --- the request -----------------------------------------------------------


def test_judgement_request_accepts_the_two_ids() -> None:
    request = JudgementRequest(lead_id=1656, note_id=10)
    assert (request.lead_id, request.note_id) == (1656, 10)


def test_judgement_request_forbids_extra_fields() -> None:
    # Note text is never accepted in a body (Design A: the note is already
    # saved and is fetched by id). A caller posting it must be told, not
    # silently ignored.
    with pytest.raises(ValidationError) as exc:
        JudgementRequest(lead_id=1, note_id=2, note="Called, no answer.")

    errors = exc.value.errors(include_url=False, include_input=False)
    assert [e["type"] for e in errors] == ["extra_forbidden"]
    assert [e["loc"] for e in errors] == [("note",)]


# --- model-output schemas --------------------------------------------------


def test_no_output_schema_carries_a_band_or_a_total() -> None:
    # The structural guarantee: a model supplies marks and a classification.
    # total/denominator/band/decision are computed in code from TenantConfig.
    for schema in _OUTPUT_SCHEMAS:
        fields = set(schema.model_fields)
        assert "band" not in fields, schema.__name__
        assert "total" not in fields, schema.__name__
        assert "denominator" not in fields, schema.__name__


def test_no_request_schema_carries_a_band_or_a_total_either() -> None:
    """The same guarantee, on the bodies a CALLER writes.

    DECISION[DIRECT_ROUTE] admits note TEXT into a request body. It admits
    nothing else: a `score`, a `band`, a `total` or a `mark` on the direct
    body would be the CRM handing us the answer, which is the same failure as
    a model handing it to us and is refused in the same way -- by there being
    no field to put it in.
    """
    for schema in _REQUEST_SCHEMAS:
        fields = set(schema.model_fields)
        assert "band" not in fields, schema.__name__
        assert "total" not in fields, schema.__name__
        assert "denominator" not in fields, schema.__name__
        assert "score" not in fields, schema.__name__
        assert "marks" not in fields, schema.__name__


def test_every_request_schema_forbids_extra_fields() -> None:
    # Including the direct body: the exception is note text on one route, not
    # "whatever the CRM feels like sending".
    for schema in _REQUEST_SCHEMAS:
        assert schema.model_config.get("extra") == "forbid", schema.__name__


# --- the direct route's body (DECISION[DIRECT_ROUTE]) ----------------------


def _direct_body(**overrides: object) -> dict:
    body: dict = {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": "Called the client, discussed the New Cairo 3BR.",
        "lead": {"leadType": "buyer", "enquiryType": "sale"},
    }
    body.update(overrides)
    return body


def test_the_direct_body_takes_the_note_and_the_four_lead_fields() -> None:
    request = DirectJudgementRequest(**_direct_body())
    assert request.note_text.startswith("Called the client")
    assert request.author_id == 27
    assert request.lead.leadType == "buyer"
    # Not sent, and not required: every lead field is nullable in the backend
    # contract, so an absent one is None rather than a validation failure.
    assert request.lead.project is None
    assert request.lead.status is None


def test_the_direct_body_forbids_an_extra_field() -> None:
    with pytest.raises(ValidationError) as exc:
        DirectJudgementRequest(**_direct_body(note="a second copy of the text"))

    errors = exc.value.errors(include_url=False, include_input=False)
    assert [e["type"] for e in errors] == ["extra_forbidden"]


def test_the_lead_context_forbids_an_extra_field() -> None:
    # The CRM cannot widen what reaches a prompt by adding fields to `lead`.
    with pytest.raises(ValidationError) as exc:
        LeadContext(leadType="buyer", name="Jane Doe")

    errors = exc.value.errors(include_url=False, include_input=False)
    assert [e["type"] for e in errors] == ["extra_forbidden"]


def test_the_direct_body_caps_note_text_at_the_hard_ceiling() -> None:
    # The hard ceiling is a REQUEST bound: over it, nothing is fingerprinted,
    # nothing is reserved and the pipeline is never entered.
    assert MAX_NOTE_TEXT_CHARS == 4000
    at_the_limit = DirectJudgementRequest(**_direct_body(note_text="x" * 4000))
    assert len(at_the_limit.note_text) == 4000

    with pytest.raises(ValidationError) as exc:
        DirectJudgementRequest(**_direct_body(note_text="x" * 4001))

    errors = exc.value.errors(include_url=False, include_input=False)
    assert [e["type"] for e in errors] == ["string_too_long"]


def test_the_direct_body_still_requires_both_ids_and_an_author() -> None:
    with pytest.raises(ValidationError):
        DirectJudgementRequest(
            lead_id=1, note_id=2, note_text="A note long enough.", lead=LeadContext()
        )


def test_every_output_schema_forbids_extra_fields() -> None:
    for schema in _OUTPUT_SCHEMAS:
        assert schema.model_config.get("extra") == "forbid", schema.__name__


def test_classification_accepts_a_note_type_or_unclassifiable() -> None:
    assert ClassificationOutput(note_type="discovery").note_type is NoteType.DISCOVERY
    assert ClassificationOutput(note_type=UNCLASSIFIABLE).note_type == "unclassifiable"


def test_classification_rejects_an_invented_type() -> None:
    with pytest.raises(ValidationError):
        ClassificationOutput(note_type="probably_a_viewing")


def test_score_output_takes_marks_and_nothing_else() -> None:
    parsed = ScoreOutput(marks={"what_happened": 20, "clarity": 5})
    assert parsed.marks[ComponentName.WHAT_HAPPENED] == 20


def test_score_output_leaves_mark_bounds_to_the_scoring_code() -> None:
    # 0 <= mark <= weight[c] is per-tenant and validate_output has no context
    # channel, so the bound is checked against TenantConfig where the score is
    # computed -- not declared here against a constant that would be wrong for
    # any tenant whose weights differ.
    assert ScoreOutput(marks={"clarity": 900}).marks[ComponentName.CLARITY] == 900
    assert ScoreOutput(marks={"clarity": -4}).marks[ComponentName.CLARITY] == -4


def test_score_output_rejects_an_unknown_component() -> None:
    with pytest.raises(ValidationError):
        ScoreOutput(marks={"enthusiasm": 5})


# --- the vagueness biconditional, both directions --------------------------


def test_vague_output_accepts_vague_with_components_and_a_prompt() -> None:
    parsed = VagueOutput(
        is_vague=True,
        missing_components=["next_step_with_date"],
        clarification_prompt="When are you following up with this client?",
        reasoning="No follow-up date is stated.",
    )
    assert parsed.missing_components == [MissingComponent.NEXT_STEP_WITH_DATE]


def test_vague_output_accepts_not_vague_with_nothing_missing() -> None:
    parsed = VagueOutput(
        is_vague=False,
        missing_components=[],
        clarification_prompt=None,
        reasoning="All three components are present.",
    )
    assert parsed.clarification_prompt is None


def test_vague_without_components_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VagueOutput(
            is_vague=True,
            missing_components=[],
            clarification_prompt="What happened?",
            reasoning="x",
        )


def test_vague_without_a_prompt_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VagueOutput(
            is_vague=True,
            missing_components=["client_said"],
            clarification_prompt=None,
            reasoning="x",
        )


def test_not_vague_with_components_is_rejected() -> None:
    with pytest.raises(ValidationError):
        VagueOutput(
            is_vague=False,
            missing_components=["client_said"],
            clarification_prompt=None,
            reasoning="x",
        )


def test_not_vague_with_a_prompt_is_rejected() -> None:
    # The dangerous half: this prompt would otherwise be sent to a salesperson
    # about a note we just judged fine.
    with pytest.raises(ValidationError):
        VagueOutput(
            is_vague=False,
            missing_components=[],
            clarification_prompt="What did the client say?",
            reasoning="x",
        )


# --- the clarification prompt cap ------------------------------------------


def test_clarification_prompt_accepts_exactly_three_hundred_chars() -> None:
    parsed = VagueOutput(
        is_vague=True,
        missing_components=["what_happened"],
        clarification_prompt="q" * 300,
        reasoning="x",
    )
    assert parsed.clarification_prompt is not None
    assert len(parsed.clarification_prompt) == 300


def test_clarification_prompt_over_the_cap_is_rejected() -> None:
    with pytest.raises(ValidationError) as exc:
        VagueOutput(
            is_vague=True,
            missing_components=["what_happened"],
            clarification_prompt="q" * 301,
            reasoning="x",
        )
    types = {
        e["type"] for e in exc.value.errors(include_url=False, include_input=False)
    }
    assert "string_too_long" in types


def test_empty_clarification_prompt_is_rejected() -> None:
    # "" is not "no prompt" -- absence is None. An empty string would reach a
    # salesperson as a blank question.
    with pytest.raises(ValidationError):
        VagueOutput(
            is_vague=True,
            missing_components=["what_happened"],
            clarification_prompt="",
            reasoning="x",
        )


# --- response shapes -------------------------------------------------------


def test_note_analysis_defaults_are_the_suppressed_shape() -> None:
    # A suppressed judgement's analysis: everything null/empty, note_type
    # carrying the classifier's answer when there was one.
    analysis = NoteAnalysis()
    assert analysis.note_type is None
    assert analysis.is_vague is None
    assert analysis.missing_components == []
    assert analysis.clarification_prompt is None
    assert analysis.reasoning is None


def test_note_analysis_missing_components_default_is_not_shared() -> None:
    first = NoteAnalysis()
    first.missing_components.append(MissingComponent.CLIENT_SAID)
    assert NoteAnalysis().missing_components == []


def test_note_analysis_can_carry_unclassifiable() -> None:
    assert NoteAnalysis(note_type=UNCLASSIFIABLE).note_type == "unclassifiable"


def test_suppressed_pairs_a_reason_with_a_detail_code() -> None:
    suppressed = Suppressed(
        reason=SuppressedReason.INSUFFICIENT_EVIDENCE,
        detail_code=SuppressedDetail.NOTE_TOO_SHORT,
    )
    assert suppressed.reason is SuppressedReason.INSUFFICIENT_EVIDENCE
    assert suppressed.detail_code is SuppressedDetail.NOTE_TOO_SHORT


def test_decision_prompt_withheld_defaults_to_none() -> None:
    decision = Decision(
        action=DecisionAction.ACCEPT_SILENT,
        prompt_sent=False,
        attempt=1,
        attempts_remaining=0,
    )
    assert decision.prompt_withheld is None
