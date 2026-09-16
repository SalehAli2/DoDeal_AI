"""Classification — pass one of three: what kind of interaction is this note.

It runs first because everything after it is chosen BY the answer. The vague
template is per type (Phase E) and the applicable components are per type
(Phase F), so neither can be assembled until the type is known. That is the
whole reason this is a separate call rather than one prompt asking for
everything: a single prompt would have to describe every type's bar at once,
and a wrong type would silently take the wrong rubric with it.

TWO ANSWERS END THE JUDGEMENT HERE, and both are states rather than scores:

  system_event    ASSUMPTION[Q6] -- the notes feed is assumed to carry
                  CRM-generated timeline entries as well as things a human
                  typed. Those are not a salesperson's work, so scoring them
                  would put the CRM's own bookkeeping into a salesperson's
                  average. They suppress as not_scorable.

                  CORRECTION PATH if the backend confirms the feed is
                  notes-only: nothing here changes and nothing is rescored. The
                  member stays in the vocabulary and the classifier simply never
                  emits it -- a suppressed judgement carries no score, so there
                  is no history to revisit. If instead the backend confirms the
                  events arrive with a machine-readable flag, the branch moves
                  from the classifier's answer to that flag and this call is
                  skipped for them.

  unclassifiable  the model's escape hatch. Offering it is what stops a note
                  that fits nothing from being forced into the nearest type and
                  scored against a rubric that does not apply to it.

The prompt names the seven types and their criteria; it names no weight, no
threshold and no band, because a classifier that knew what a type was worth
would be answering a question we did not ask.
"""

from __future__ import annotations

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_CLASSIFY
from dodeal_ai.core.prompting import AssembledPrompt, build_prompt
from dodeal_ai.schemas.lead import Lead, LeadNote
from dodeal_ai.units.structured_intelligence.llm_call import call_model
from dodeal_ai.units.structured_intelligence.schemas import (
    UNCLASSIFIABLE,
    ClassificationOutput,
    ClassifierOutput,
    NoteType,
    SuppressedDetail,
)

CLASSIFY_TEMPLATE = "structured_intelligence/classify_v1.txt"
CLASSIFY_LABEL = "llm.unit_a.classify"

# What this task's answer may cost (register item 15). The answer is one object
# with one field, and that field's value comes from a fixed ASCII vocabulary:
# {"note_type": "no_contact"} is 27 characters. NOTHING here scales with the
# note, so unlike vague detection this ceiling does not move with the note's
# language -- an Arabic note and an English one produce the same eleven-token
# answer. The headroom is for a model that prefaces or fences its reply: that
# still fits, and is then rejected as MALFORMED, which is a diagnosis. A ceiling
# tight enough to truncate it would report the same fault as truncation.
CLASSIFY_MAX_OUTPUT_TOKENS = 64

# What the classifier is allowed to see of the lead, and nothing else. Four
# fields that help place an ambiguous note ("interested in the same one" reads
# differently on a leasing enquiry than on a sale) and cannot identify the
# client: no name, no phone, no email. The tool layer fetched a whole Lead; the
# prompt gets four of its fields.
_LEAD_CONTEXT_FIELDS = ("leadType", "enquiryType", "project", "status")

# What a null backend field becomes in the prompt. Every lead field except `id`
# may be null, and the backend has confirmed its data is frequently incomplete,
# so absence is the common case rather than an anomaly. A fixed word for it
# keeps the section's shape identical across leads.
_UNKNOWN = "unknown"


def _caller_data(note: LeadNote, lead: Lead) -> str:
    """The untrusted section: lead context first, the note last.

    ORDER MATTERS, for one reason that is not style. Both parts are untrusted --
    the note was typed by a salesperson and the context came from a backend we
    do not control -- but the note is the part being ANALYSED. Putting it last
    means "everything after NOTE:" is unambiguous, so a note that contains the
    string "LEAD CONTEXT:" cannot appear to precede context that outranks it.

    Delimiter neutralisation is build_prompt's job and is not repeated here:
    doing it in two places is how one of them drifts.
    """
    lines = [
        f"{field}: {getattr(lead, field, None) or _UNKNOWN}"
        for field in _LEAD_CONTEXT_FIELDS
    ]
    context = "\n".join(lines)
    return f"LEAD CONTEXT:\n{context}\n\nNOTE:\n{note.note}"


def build_classification_prompt(note: LeadNote, lead: Lead) -> AssembledPrompt:
    """The assembled classification prompt. Separate from the call so a test can
    inspect what would be sent without scripting a response for it."""
    return build_prompt(CLASSIFY_TEMPLATE, _caller_data(note, lead))


async def classify(
    client: LLMClient,
    note: LeadNote,
    lead: Lead,
    *,
    scope: TenantScope,
    settings: Settings,
    reprompt: bool = True,
) -> tuple[ClassificationOutput, LLMResponse]:
    """One model call, or two if the first answer is malformed. Returns the
    validated answer and the raw response, whose `model` is stamped on the
    judgement even when the answer suppresses it."""
    return await call_model(
        client,
        build_classification_prompt(note, lead),
        ClassificationOutput,
        CLASSIFY_LABEL,
        scope=scope,
        settings=settings,
        profile=PROFILE_UNIT_A_CLASSIFY,
        max_output_tokens=CLASSIFY_MAX_OUTPUT_TOKENS,
        reprompt=reprompt,
    )


def suppression_for(note_type: ClassifierOutput) -> SuppressedDetail | None:
    """The detail code this classification suppresses under, or None to carry on.

    Returns the DETAIL only; the reason is always `not_scorable` and the
    judgement itself is built by the pipeline, which owns that shape. Two
    answers stop here and five continue -- and a new NoteType that should stop
    is one line, in the one place the question is asked.
    """
    if note_type == NoteType.SYSTEM_EVENT:
        return SuppressedDetail.SYSTEM_EVENT
    if note_type == UNCLASSIFIABLE:
        return SuppressedDetail.UNCLASSIFIABLE
    return None
