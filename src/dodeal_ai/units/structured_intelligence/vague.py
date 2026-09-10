"""Vague detection — pass two: is this note enough to work from, and if not,
what is the one question that would fix it.

SIX PROMPTS, NOT ONE. The bar is different per note type and there is no honest
way to write it once: a no_contact note that says only "no answer" is complete,
while a discovery note that says only "went well" is useless. A single template
would have to describe all six bars and then trust the model to pick the right
one after we have ALREADY established which one applies -- paying twice for the
same decision and giving it a second chance to get it wrong.

WHAT THE MODEL DECIDES AND WHAT IT DOES NOT. It decides whether the note clears
the bar, which of a FIXED list of three things is missing, and what to ask. It
does not decide what that costs: no weight, no threshold and no band appears in
any of these templates, and `is_vague` is not a score. Scoring is a separate
call with a separate template (Phase F), and the decision is arithmetic in code.

THE CLARIFICATION PROMPT IS THE ONLY PART THAT REACHES A PERSON. Everything else
this unit produces is read by the CRM; this one string is shown to the
salesperson who wrote the note. That is why the templates forbid "please improve
this note" explicitly, why the schema caps it at 300 characters, and why a vague
answer with no question is a REJECTED answer rather than a prompt we invent.

THE PER-TYPE RESTRICTION. `no_contact` is the one type whose allowed set is
narrower than the vocabulary: the client was not reached, so `client_said`
cannot be missing -- there is nothing they said. A model that reports it anyway
has misread the note, and asking its author what the client said would be asking
them to invent it. The set lives in TenantConfig (`allowed_missing_by_type`,
built in Phase A) rather than here, because it is a rubric decision like every
other one, and a tenant that wants a different set should not need a code change.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

from dodeal_ai.core.config import Settings
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_VAGUE
from dodeal_ai.core.prompting import AssembledPrompt, PromptError, build_prompt
from dodeal_ai.schemas.lead import LeadNote
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.llm_call import call_model, output_rejected
from dodeal_ai.units.structured_intelligence.schemas import NoteType, VagueOutput

VAGUE_LABEL = "llm.unit_a.vague"

# What this task's answer may cost (register item 15), and THE ONE OF THE THREE
# CEILINGS SIZED AGAINST ARABIC. It is the only pass whose answer carries free
# text back: `clarification_prompt` is written in the language of the note, and
# `reasoning` is one or two sentences the schema does not cap at all.
#
# Sized on the longest ARABIC answer, never the English one. Arabic runs roughly
# 2-3x the tokens per word that English does, so a ceiling that fits an English
# answer with room to spare truncates an ordinary Arabic one -- and truncation
# is MALFORMED here (llm_call.parse_output). An English-sized ceiling would not
# degrade an Arabic note gracefully: it would spend the note's one reprompt and
# then 503 a judgement that was never in doubt, for every Arabic note, which is
# most of them.
#
# The arithmetic, worst case: ~40 tokens of structure (four keys plus the fixed
# ASCII component names), up to ~400 for 300 characters of Arabic in the
# clarification prompt (the schema's hard cap), up to ~400 for two Arabic
# sentences of reasoning, and the remainder is slack for a model that formats
# the object across lines. Settings.llm_max_output_tokens carries the same
# headroom for the same reason; this task is why.
VAGUE_MAX_OUTPUT_TOKENS = 1024

# Six types, six templates. system_event is DELIBERATELY ABSENT: a machine
# timeline entry is suppressed by the classifier and never reaches this pass, so
# there is no seventh template to keep in step. A test asserts the keys plus
# system_event are exactly NoteType, which is what makes a future eighth type a
# failing test rather than a KeyError in production.
_TEMPLATES: Mapping[NoteType, str] = MappingProxyType(
    {
        NoteType.NO_CONTACT: "structured_intelligence/vague_no_contact_v1.txt",
        NoteType.CALLBACK: "structured_intelligence/vague_callback_v1.txt",
        NoteType.DISCOVERY: "structured_intelligence/vague_discovery_v1.txt",
        NoteType.VIEWING: "structured_intelligence/vague_viewing_v1.txt",
        NoteType.NEGOTIATION: "structured_intelligence/vague_negotiation_v1.txt",
        NoteType.WON_LOST: "structured_intelligence/vague_won_lost_v1.txt",
    }
)


def template_for(note_type: NoteType) -> str:
    """This type's template filename.

    A type with no template is a programming error, not a caller error: the only
    one is system_event, and the classifier stops before this pass for it. It
    raises rather than falling back to a generic template, because a generic
    template would score a no_contact note against a discovery bar and nobody
    would see it happen.
    """
    try:
        return _TEMPLATES[note_type]
    except KeyError:
        raise PromptError("no vague template for this note type") from None


def _caller_data(note: LeadNote) -> str:
    """The untrusted section: the note, and nothing else.

    No lead context here, unlike classification. The floor test the templates
    apply is "could another agent read THIS NOTE and carry on" -- handing the
    model the lead's project and status would let it fill in from the record
    what the note itself does not say, and pass a note that leaves the next
    reader guessing.
    """
    return f"NOTE:\n{note.note}"


def build_vague_prompt(note: LeadNote, note_type: NoteType) -> AssembledPrompt:
    """The assembled prompt for this type. Separate from the call so a test can
    inspect what would be sent without scripting a response for it."""
    return build_prompt(template_for(note_type), _caller_data(note))


def allowed_components_check(
    note_type: NoteType, config: TenantConfig
) -> Callable[[VagueOutput], None]:
    """The rule the schema cannot hold: which components may be reported missing
    for THIS type, under THIS tenant's rubric.

    `validate_output` takes a schema and a raw value and has no context channel,
    so this cannot be a field constraint. It runs inside the validated call
    (llm_call.parse_output's `check`) rather than after it, so a violation is an
    OutputValidationError like any other malformed answer and earns the single
    reprompt.

    The offending component NAME goes into the error location. That is safe: it
    is a MissingComponent member, fixed vocabulary, never free text.
    """
    allowed = config.allowed_missing_by_type[note_type]

    def check(output: VagueOutput) -> None:
        offenders = [c for c in output.missing_components if c not in allowed]
        if offenders:
            raise output_rejected(
                VAGUE_LABEL,
                tuple(
                    (f"missing_components.{c.value}", "component_not_allowed_for_type")
                    for c in offenders
                ),
            )

    return check


async def detect_vagueness(
    client: LLMClient,
    note: LeadNote,
    note_type: NoteType,
    *,
    config: TenantConfig,
    settings: Settings,
) -> tuple[VagueOutput, LLMResponse]:
    """One model call against this type's template, or two if the first answer
    is malformed. Returns the validated answer and the raw response, whose
    `model` the judgement is stamped with."""
    return await call_model(
        client,
        build_vague_prompt(note, note_type),
        VagueOutput,
        VAGUE_LABEL,
        settings=settings,
        profile=PROFILE_UNIT_A_VAGUE,
        max_output_tokens=VAGUE_MAX_OUTPUT_TOKENS,
        check=allowed_components_check(note_type, config),
    )
