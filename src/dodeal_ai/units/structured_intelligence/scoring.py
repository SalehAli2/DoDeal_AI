"""Scoring — pass three, and the arithmetic that turns marks into a judgement.

THE DIVISION OF LABOUR IS THE WHOLE DESIGN. The model returns a mark per
applicable component and knows nothing else: not what a component is worth, not
what the marks add up to, not what the result is called. The total, the
denominator and the band are computed HERE, from marks plus TenantConfig. A
model cannot hand us a score, and the templates carry no weight, no threshold
and no band for it to read.

Why that matters beyond tidiness: a band is a judgement about a salesperson's
work, and the only thing that makes it defensible is that it is reproducible
from the marks, the config and the four version stamps. The moment a band could
arrive from a model, the same note could carry two different bands with no
recorded reason.

SUPPRESSION IS NOT A ZERO. A component that does not apply leaves the
DENOMINATOR; it is not marked 0. Scoring a no_contact note 0 for "what the
client said" would cap an honest note at 60/100 and teach salespeople to pad
notes with invented conversation. `applicable_components` decides what counts,
and everything downstream follows from it.

THE THREE WAYS MARKS CAN BE WRONG -- a mark for a component that does not apply,
a missing mark for one that does, and a mark outside [0, weight] -- are all
`OutputValidationError`. They are enforced through `llm_call.parse_output`'s
check hook, INSIDE the validated call, because the single reprompt is
defined over that exception: a bound checked after the call returned would be a
malformed answer that never earned its reprompt. And they cannot be schema
constraints, because the bound is the TENANT's weight and `validate_output` has
no context channel.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from dodeal_ai.core.config import Settings
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_SCORE
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.schemas.lead import LeadNote
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.llm_call import call_model, output_rejected
from dodeal_ai.units.structured_intelligence.schemas import (
    ComponentName,
    NoteScore,
    NoteType,
    ScoreComponent,
    ScoreOutput,
)

SCORE_TEMPLATE = "structured_intelligence/score_v1.txt"
SCORE_LABEL = "llm.unit_a.score"

# What this task's answer may cost (register item 15). Five fixed ASCII keys and
# five whole numbers -- around 110 characters, or roughly twice that if the
# model formats the object across lines. Like classification and unlike vague
# detection, NOTHING in this answer comes back in the note's language, so the
# ceiling does not move with Arabic. The headroom is for formatting and for a
# fenced reply, which fits and is then rejected as malformed rather than
# truncated: two faults that would otherwise be reported as one.
SCORE_MAX_OUTPUT_TOKENS = 256


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


def validate_marks(
    marks: Mapping[ComponentName, int], note_type: NoteType, config: TenantConfig
) -> None:
    """Reject marks that do not answer the question that was asked.

    Every problem is reported, not just the first: one reprompt is all a model
    gets, so it should be told everything that was wrong with the answer rather
    than being corrected one field at a time across attempts it will not have.

    The component NAME appears in the error location. That is safe -- it is a
    `ComponentName` member, fixed vocabulary, never free text -- and it is what
    makes a rejected answer diagnosable without logging the answer.
    """
    applicable = applicable_components(note_type, config)
    problems: list[tuple[str, str]] = []

    for component in ComponentName:
        location = f"marks.{component.value}"
        mark = marks.get(component)
        if component in applicable:
            if mark is None:
                # An unmarked applicable component is not a zero. Treating it as
                # one would silently mark a note down for the model's omission.
                problems.append((location, "missing_mark"))
            elif not 0 <= mark <= config.weights[component]:
                problems.append((location, "mark_out_of_range"))
        elif mark is not None:
            # The weight already left the denominator; accepting the mark would
            # either be ignored (so why accept it) or quietly change the result.
            problems.append((location, "mark_for_suppressed_component"))

    if problems:
        raise output_rejected(SCORE_LABEL, tuple(problems))


def marks_check(
    note_type: NoteType, config: TenantConfig
) -> Callable[[ScoreOutput], None]:
    """The check hook `llm_call.call_model` runs inside the validated call, so a
    bad mark fails exactly like a bad shape and earns the same single reprompt."""

    def check(output: ScoreOutput) -> None:
        validate_marks(output.marks, note_type, config)

    return check


def compute_score(
    marks: Mapping[ComponentName, int], note_type: NoteType, config: TenantConfig
) -> NoteScore:
    """Marks -> total, denominator, band and the five-row breakdown.

    The arithmetic, in full:

        applicable  = components not suppressed by type and not suppressed by Q13
        denominator = sum of the applicable weights   (100 / 80 / 60 today)
        raw         = sum of the applicable marks
        total       = (raw * 100 + denominator // 2) // denominator

    That last line is integer round-half-up, done in integers throughout: no
    float ever touches a score, so the result cannot depend on binary rounding
    and two runs of the same marks cannot differ. 55/80 is 68.75 and becomes 69
    (fair); 56/80 is 70.0 and becomes 70 (good) -- one mark either side of a band
    boundary, which is exactly where a float would be an unpleasant surprise.

    `components` lists ALL FIVE in fixed order, suppressed ones carrying
    `mark: null, suppressed: true`. `suppressed` means the weight left the
    denominator; it does not mean the mark was zero, and the two are different
    outcomes.
    """
    validate_marks(marks, note_type, config)
    applicable = applicable_components(note_type, config)

    denominator = sum(config.weights[component] for component in applicable)
    if denominator == 0:
        # Unreachable with any shipped TenantConfig -- clarity is suppressed by
        # no type and is not Q13-gated. Loud rather than a ZeroDivisionError,
        # because the only way here is a rubric that suppresses everything, and
        # that is a configuration fault worth naming.
        raise ValueError("tenant rubric leaves no applicable component")

    raw = sum(marks[component] for component in applicable)
    total = (raw * 100 + denominator // 2) // denominator

    return NoteScore(
        total=total,
        band=config.band_for(total),
        denominator=denominator,
        components=[
            ScoreComponent(
                name=component,
                mark=marks[component] if component in applicable else None,
                weight=config.weights[component],
                suppressed=component not in applicable,
            )
            for component in ComponentName
        ],
    )


def _caller_data(
    note: LeadNote, applicable: tuple[ComponentName, ...], config: TenantConfig
) -> str:
    """The untrusted section: which components to mark and their ceilings, then
    the note.

    THE WEIGHTS TRAVEL IN THE CALLER DATA, NOT IN THE TEMPLATE, and that is not
    an oversight. A weight in template text could not be changed without bumping
    the prompt version, and a per-tenant rubric would need a per-tenant template.
    Putting the numbers in the variable half keeps `.stable` byte-identical for
    every tenant and every note type -- one cached prefix, one file to review.

    They are ceilings, not scores: the model is told the highest mark each
    component can take, never what the marks add up to or what the result is
    called.

    The components block goes FIRST and the note LAST, as in classification. A
    note that names its own components is a note trying to mark itself, and the
    template says in so many words that only the block above it counts. The
    bound is enforced in code regardless -- `validate_marks` rejects anything
    outside [0, weight] -- so a persuasive note cannot inflate a mark; the
    ordering just means the model is not asked to arbitrate.
    """
    ceilings = "\n".join(
        f"{component.value}: 0 to {config.weights[component]}"
        for component in applicable
    )
    return f"COMPONENTS TO MARK:\n{ceilings}\n\nNOTE:\n{note.note}"


def build_score_prompt(
    note: LeadNote, note_type: NoteType, config: TenantConfig
) -> AssembledPrompt:
    """The assembled scoring prompt. Separate from the call so a test can inspect
    what would be sent without scripting a response for it."""
    return build_prompt(
        SCORE_TEMPLATE,
        _caller_data(note, applicable_components(note_type, config), config),
    )


async def score_note(
    client: LLMClient,
    note: LeadNote,
    note_type: NoteType,
    *,
    config: TenantConfig,
    settings: Settings,
) -> tuple[ScoreOutput, LLMResponse]:
    """One model call, or two if the first answer is malformed. Returns the
    validated marks -- already bounded against this tenant's weights by the
    check hook -- and the raw response, whose `model` the judgement is stamped
    with."""
    return await call_model(
        client,
        build_score_prompt(note, note_type, config),
        ScoreOutput,
        SCORE_LABEL,
        settings=settings,
        profile=PROFILE_UNIT_A_SCORE,
        max_output_tokens=SCORE_MAX_OUTPUT_TOKENS,
        check=marks_check(note_type, config),
    )
