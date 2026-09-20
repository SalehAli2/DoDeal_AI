"""Scoring — pass three, and the arithmetic that turns check answers into a
judgement.

THE DIVISION OF LABOUR IS THE WHOLE DESIGN. The model answers yes or no to a
fixed list of checks and knows nothing else: not which component a check
belongs to, not what it is worth, not what the answers add up to, not what the
result is called. The marks, the total, the denominator and the band are
computed HERE, from check answers plus TenantConfig. A model cannot hand us a
score, and the templates carry no weight, no threshold and no band for it to
read.

WHY CHECKS RATHER THAN MARKS (register item 131). A model asked for a whole
number out of 25 cannot use that resolution consistently: the same note gets
19 one day and 22 the next, and the rubric's own acceptance target is band
agreement with a human. Binary criteria are the most reliable form of rubric
judgement, so the model answers observable facts and the arithmetic stays where
arithmetic belongs. It also makes a mark explainable: a salesperson can be
shown which check failed, not just a number.

Why that matters beyond tidiness: a band is a judgement about a salesperson's
work, and the only thing that makes it defensible is that it is reproducible
from the answers, the config and the four version stamps. The moment a band
could arrive from a model, the same note could carry two different bands with
no recorded reason.

SUPPRESSION IS NOT A ZERO. A component that does not apply leaves the
DENOMINATOR; it is not marked 0. Scoring a no_contact note 0 for "what the
client said" would cap an honest note at 60/100 and teach salespeople to pad
notes with invented conversation. `applicable_components` decides what counts,
`applicable_checks` decides what is even asked, and everything downstream
follows from them.

THE TWO WAYS AN ANSWER CAN BE WRONG -- an answer for a check that was not
asked, and a missing answer for one that was -- are both
`OutputValidationError`. They are enforced through `llm_call.parse_output`'s
check hook, INSIDE the validated call, because the single reprompt is defined
over that exception: a fault found after the call returned would be a malformed
answer that never earned its reprompt. And they cannot be schema constraints,
because which checks apply depends on the TENANT's rubric and the note's type,
and `validate_output` has no context channel.

There is no third way. A bool cannot be out of range, which is one fault class
that disappeared with the marks.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_SCORE
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.schemas.lead import LeadNote
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.llm_call import call_model, output_rejected
from dodeal_ai.units.structured_intelligence.schemas import (
    CheckName,
    ComponentName,
    NoteScore,
    NoteType,
    ScoreComponent,
    ScoreOutput,
)

SCORE_TEMPLATE = "structured_intelligence/score_v2.txt"
SCORE_LABEL = "llm.unit_a.score"

# What this task's answer may cost (register item 15, revised by 131). Twelve
# fixed ASCII keys with true or false, plus one short sentence of reasoning.
# The reasoning is written in English whatever the note's language, so the
# ceiling does not move with Arabic; the headroom is for formatting and for a
# fenced reply, which fits and is then rejected as malformed rather than
# truncated.
SCORE_MAX_OUTPUT_TOKENS = 384


def applicable_components(
    note_type: NoteType, config: TenantConfig
) -> tuple[ComponentName, ...]:
    """The components that count for this note type, in the fixed response order.

    Two independent reasons a component drops out, and they compose:

      BY TYPE      the rubric says this kind of note cannot answer it. A
                   no_contact note has no client_said and no deal_specifics --
                   the client was never reached.

      ASSUMPTION[Q13]  no field on the lead is CONFIRMED to carry the business
                   line, so there is no per-line checklist to mark
                   `deal_specifics` against, and marking it anyway would be
                   marking a guess. `deal_specifics_applicable` is False and the
                   component leaves the denominator for EVERY type (100 -> 80).

                   CORRECTION PATH when the backend names the field: set
                   `business_line_field`, flip `deal_specifics_applicable` to
                   True, add the checklist the prompt needs, bump
                   `config_version` -- and NEVER rescore history. Old judgements
                   carry the old stamp and a denominator of 80; new ones carry
                   100. That they are distinguishable is the point of the stamp.

    Iterating `ComponentName` rather than the config's mapping is deliberate: the
    enum's declaration order IS the response order, so the caller can rely on
    `components[2]` being next_step_date without matching on name.
    """
    suppressed_by_type = config.suppressed_components_by_type.get(
        note_type, frozenset()
    )
    return tuple(
        component
        for component in ComponentName
        if component not in suppressed_by_type
        and (
            component is not ComponentName.DEAL_SPECIFICS
            or config.deal_specifics_applicable
        )
    )


def applicable_checks(
    note_type: NoteType, config: TenantConfig
) -> tuple[CheckName, ...]:
    """The checks to ask about, for this type under this tenant's rubric.

    A suppressed component's checks are never asked and never answered. The
    model is not given a question whose answer is thrown away, and the reply
    stays short.
    """
    return tuple(
        check
        for component in applicable_components(note_type, config)
        for check in config.checks_by_component[component]
    )


def validate_checks(
    checks: Mapping[CheckName, bool], note_type: NoteType, config: TenantConfig
) -> None:
    """Reject answers that do not answer the question that was asked.

    Two faults, both reported together: a missing answer for a check that was
    asked, and an answer for one that was not. There is no range fault any
    more -- a bool cannot be out of range, which is one of the reasons the
    checks replaced marks (register item 131).

    The check NAME appears in the error location. Safe: it is a CheckName
    member, fixed vocabulary, never free text.
    """
    applicable = set(applicable_checks(note_type, config))
    problems: list[tuple[str, str]] = []

    for check in CheckName:
        location = f"checks.{check.value}"
        answered = check in checks
        if check in applicable and not answered:
            problems.append((location, "missing_check"))
        elif check not in applicable and answered:
            problems.append((location, "check_for_suppressed_component"))

    if problems:
        raise output_rejected(SCORE_LABEL, tuple(problems))


def checks_check(
    note_type: NoteType, config: TenantConfig
) -> Callable[[ScoreOutput], None]:
    """The check hook `llm_call.call_model` runs inside the validated call, so a
    bad answer fails exactly like a bad shape and earns the same single
    reprompt."""

    def check(output: ScoreOutput) -> None:
        validate_checks(output.checks, note_type, config)

    return check


def _mark_for(
    component: ComponentName,
    checks: Mapping[CheckName, bool],
    config: TenantConfig,
) -> int:
    """One component's mark, from how many of its checks came back true.

    NEXT_STEP_DATE has one special rule: ns_closure true is full marks whatever
    the other two say. A lead that ended, with a reason, has no next step to
    name, and counting it as one true check out of three would mark a complete
    note down for being complete.
    """
    component_checks = config.checks_by_component[component]
    if (
        component is ComponentName.NEXT_STEP_DATE
        and checks.get(CheckName.NS_CLOSURE) is True
    ):
        return config.weights[component]
    true_count = sum(1 for check in component_checks if checks.get(check) is True)
    return config.marks_by_true_count[component][true_count]


def compute_score(
    checks: Mapping[CheckName, bool], note_type: NoteType, config: TenantConfig
) -> NoteScore:
    """Check answers -> marks -> total, denominator, band and the breakdown.

    The arithmetic, in full:

        applicable  = components not suppressed by type and not suppressed by Q13
        mark        = marks_by_true_count[component][how many of its checks are true]
        denominator = sum of the applicable weights   (100 / 80 / 60 today)
        raw         = sum of the applicable marks
        total       = (raw * 100 + denominator // 2) // denominator

    That last line is integer round-half-up, done in integers throughout: no
    float ever touches a score, so the result cannot depend on binary rounding
    and two runs of the same answers cannot differ.

    `components` lists ALL FIVE in fixed order, suppressed ones carrying
    `mark: null, suppressed: true`. `suppressed` means the weight left the
    denominator; it does not mean the mark was zero.
    """
    validate_checks(checks, note_type, config)
    applicable = applicable_components(note_type, config)

    denominator = sum(config.weights[component] for component in applicable)
    if denominator == 0:
        raise ValueError("tenant rubric leaves no applicable component")

    marks = {
        component: _mark_for(component, checks, config) for component in applicable
    }
    raw = sum(marks.values())
    total = (raw * 100 + denominator // 2) // denominator

    return NoteScore(
        total=total,
        band=config.band_for(total),
        denominator=denominator,
        components=[
            ScoreComponent(
                name=component,
                mark=marks.get(component),
                weight=config.weights[component],
                suppressed=component not in applicable,
            )
            for component in ComponentName
        ],
    )


def _caller_data(applicable: tuple[CheckName, ...], note_text: str) -> str:
    """The untrusted section: which checks to answer, then the note.

    No weights and no ceilings travel here any more (register item 131). The
    model answers facts and is told nothing about what a fact is worth, so a
    weight change needs no prompt change and a persuasive note has no number
    to aim at.

    The checks block goes FIRST and the note LAST, as in classification. A note
    that names its own checks is trying to mark itself, and the template says
    only the block above it counts.
    """
    listed = "\n".join(check.value for check in applicable)
    return f"CHECKS TO ANSWER:\n{listed}\n\nNOTE:\n{note_text}"


def build_score_prompt(
    note: LeadNote, note_type: NoteType, config: TenantConfig
) -> AssembledPrompt:
    """The assembled scoring prompt. Separate from the call so a test can inspect
    what would be sent without scripting a response for it."""
    return build_prompt(
        SCORE_TEMPLATE,
        caller_data=_caller_data(applicable_checks(note_type, config), note.note),
    )


async def score_note(
    client: LLMClient,
    note: LeadNote,
    note_type: NoteType,
    *,
    scope: TenantScope,
    config: TenantConfig,
    settings: Settings,
    reprompt: bool = True,
) -> tuple[ScoreOutput, LLMResponse]:
    """One model call, or two if the first answer is malformed. Returns the
    validated check answers -- already matched against the checks this type and
    tenant asked for -- and the raw response, whose `model` the judgement is
    stamped with."""
    return await call_model(
        client,
        build_score_prompt(note, note_type, config),
        ScoreOutput,
        SCORE_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_A_SCORE,
        max_output_tokens=SCORE_MAX_OUTPUT_TOKENS,
        check=checks_check(note_type, config),
        reprompt=reprompt,
    )
