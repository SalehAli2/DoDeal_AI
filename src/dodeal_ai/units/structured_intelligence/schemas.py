"""Unit A note-judgement contracts — the vocabularies, the model-output
schemas, and the response shape.

Three groups live here, and the boundary between them is the point:

  VOCABULARIES  StrEnums. Every code a caller or a log line can carry is a
                member of one of these. Nothing in this unit ever emits a bare
                string where an enum exists -- that is what makes reason codes
                a fixed vocabulary (ASSUMPTIONS §3.3) rather than a convention.

  MODEL OUTPUT  ClassificationOutput / VagueOutput / ScoreOutput. These are
                UNTRUSTED: they describe what we are willing to accept back
                from a model, and they are the schemas handed to
                core/validation.py::validate_output. All three are
                extra="forbid" -- a model that invents a field is a malformed
                response, not a tolerated one. (Contrast schemas/lead.py, which
                is extra="ignore" because the BACKEND is allowed to grow
                fields and we do not control it.)

  RESPONSE      NoteAnalysis / ScoreComponent / NoteScore / Suppressed /
                Decision / Enforcement / Versions / Judgement. What the CRM
                receives.

THE RULE THIS FILE ENFORCES STRUCTURALLY: no REQUEST and no model-output schema
has a `band` or a `total` field. The model supplies a classification and yes/no
check answers (register item 131); the marks, the total, the denominator, the
band and the decision are computed in code from TenantConfig. Neither a model nor a caller can hand us a score. A test asserts
this by introspecting model_fields, so adding such a field to either kind of
schema fails the suite rather than silently moving the arithmetic into the
prompt -- or into the CRM's payload, which is why the direct route's body is
covered by the same test as the three output schemas.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StringConstraints,
    model_validator,
)

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------


class NoteType(StrEnum):
    """What kind of interaction the note records.

    Six human types plus system_event. system_event is ASSUMPTION[Q6]: the
    notes feed is assumed to carry backend-generated timeline entries as well
    as things a human typed. Those are not a salesperson's work and are never
    scored -- they suppress as not_scorable. If the backend confirms the feed
    is notes-only, this member stays in the vocabulary and is simply never
    emitted; nothing needs rescoring, because a suppressed judgement carries
    no score.
    """

    NO_CONTACT = "no_contact"
    CALLBACK = "callback"
    DISCOVERY = "discovery"
    VIEWING = "viewing"
    NEGOTIATION = "negotiation"
    WON_LOST = "won_lost"
    SYSTEM_EVENT = "system_event"


# The classifier may also decline. Kept as a Literal rather than an eighth
# NoteType member: "unclassifiable" is the ABSENCE of a type, and making it a
# member would let it flow into places that legitimately expect a real type
# (suppressed_components_by_type, allowed_missing_by_type).
UNCLASSIFIABLE = "unclassifiable"

# A plain assignment alias, not a PEP 695 `type` statement: pydantic resolves
# this form on every supported version, and the models below annotate with it
# directly so the vocabulary has exactly one spelling.
ClassifierOutput = NoteType | Literal["unclassifiable"]


class MissingComponent(StrEnum):
    """What the vagueness pass may report as absent from the note.

    Deliberately NOT the same vocabulary as ComponentName: this is the
    narrower set a clarification prompt can sensibly ask about, and its
    date member is spelled next_step_with_date where the scored component is
    next_step_date. Do not merge the two enums.
    """

    WHAT_HAPPENED = "what_happened"
    CLIENT_SAID = "client_said"
    NEXT_STEP_WITH_DATE = "next_step_with_date"


class ComponentName(StrEnum):
    """The five scored components, in the fixed order the response lists them.

    Declaration order IS the response order (StrEnum preserves it, and
    __members__ is ordered), so a caller can rely on components[2] being
    next_step_date without matching on name.
    """

    WHAT_HAPPENED = "what_happened"
    CLIENT_SAID = "client_said"
    NEXT_STEP_DATE = "next_step_date"
    DEAL_SPECIFICS = "deal_specifics"
    CLARITY = "clarity"


class CheckName(StrEnum):
    """The twelve yes/no facts the model answers about a note.

    Binary criteria, not marks: a model asked for a whole number out of
    25 cannot use that resolution consistently, and the rubric's own
    acceptance target is band agreement with a human. Each check is one
    observable fact; the marks and the total are computed in code from
    TenantConfig (register item 131).

    Declaration order is component order, so a reader can see which
    checks belong together without consulting the mapping.
    """

    # what_happened
    WH_OUTCOME = "wh_outcome"
    WH_ACTION = "wh_action"
    # client_said
    CS_PRESENT = "cs_present"
    CS_OWN_TERMS = "cs_own_terms"
    # next_step_date
    NS_ACTION = "ns_action"
    NS_DATE = "ns_date"
    NS_CLOSURE = "ns_closure"
    # deal_specifics
    DS_FIGURES = "ds_figures"
    DS_SUBJECT = "ds_subject"
    DS_TIMING = "ds_timing"
    # clarity
    CL_READABLE = "cl_readable"
    CL_SUBSTANCE = "cl_substance"


class Band(StrEnum):
    """The qualitative label derived from the total. Derived in code from
    TenantConfig.band_boundaries, never accepted from a model or a caller."""

    POOR = "poor"
    FAIR = "fair"
    GOOD = "good"
    EXCELLENT = "excellent"


class DecisionAction(StrEnum):
    """What the CRM is told to do about the clarification prompt. The
    enforcement verdict is derived from it; see EnforcementVerdict."""

    ACCEPT_SILENT = "accept_silent"
    ACCEPT_FLAG_PROMPT = "accept_flag_prompt"
    PROMPT_CLARIFICATION = "prompt_clarification"


class EnforcementMode(StrEnum):
    """What a tenant has asked us to do with a judgement it does not like.

    THE VOCABULARY, not the behaviour: the verdict is derived from the mode in
    decide.py::enforcement, and `strict` does not block anything today. It
    lives here rather than in config.py because it is now a code the CRM reads
    off every judgement, which is what this section of the file is for;
    config.py imports it back and is still the only place a tenant's mode is
    chosen.

    off       we judge and report, and nothing is ever flagged.
    advisory  a note we would ask about is flagged. EVERY TENANT LAUNCHES
              HERE, and it is config.py's default.
    strict    advisory plus blocking at a stage change, which is not built.
              A team moves here only once the calibration target is met, and
              nothing in this service can check that, so this service never
              enables it -- a tenant file does.
    """

    OFF = "off"
    ADVISORY = "advisory"
    STRICT = "strict"


class EnforcementVerdict(StrEnum):
    """What the CRM should DO, stated rather than inferred.

    Three, and the CRM never has to guess between them: `allow` lets the note
    through, `flag` marks it for the salesperson or their manager, `block`
    refuses the transition it applies to. A MISSING VERDICT IS NOT ALLOW --
    the field is required on every judgement, scored or suppressed, so an
    absent block is a malformed judgement and not a quiet permission.
    """

    ALLOW = "allow"
    FLAG = "flag"
    BLOCK = "block"


class EnforcementTarget(StrEnum):
    """What a verdict is about. One member today: the note itself.

    A separate vocabulary rather than a bare string because the blocking work
    (a stage change) will add a second member, and a caller matching on
    `applies_to` must keep working when it does.
    """

    NOTE = "note"


class PromptWithheld(StrEnum):
    """Why a clarification prompt was computed but not sent.

    The field this fills is `PromptWithheld | None`; None means nothing was
    withheld. A withheld prompt is a STATE with a reason, never a silently
    absent prompt -- the CRM can tell "we had nothing to ask" from "the user
    is rate limited" without inferring it.
    """

    # A history judgement (register item 127) never asks: the note is old and
    # its author is not waiting on it. First in decide()'s order.
    HISTORY = "history"
    RESUBMISSION = "resubmission"
    ATTEMPT_CAP = "attempt_cap"
    RATE_LIMITED = "rate_limited"
    NOTHING_TO_ASK = "nothing_to_ask"


class SuppressedReason(StrEnum):
    """Why no score was produced. Suppression is a STATE, never a zero, never
    a null-treated-as-zero, and never a `poor` band -- a suppressed judgement
    carries `score: null`, so it cannot be averaged into anything."""

    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    NOT_SCORABLE = "not_scorable"


class SuppressedDetail(StrEnum):
    """The specific cause under a SuppressedReason.

    NOTE_TOO_SHORT pairs with INSUFFICIENT_EVIDENCE. The other three pair with
    NOT_SCORABLE: SYSTEM_EVENT and UNCLASSIFIABLE come from the classifier -- a
    machine timeline entry (ASSUMPTION[Q6]) and a note it could not place at
    all -- and NOTE_TOO_LONG comes from the length gate, before any model call.

    NOTE_TOO_LONG is NOT_SCORABLE rather than INSUFFICIENT_EVIDENCE because the
    problem is not that there is too little to judge: there is too much to judge
    as ONE note, and the rubric scores a single interaction. It takes no third
    SuppressedReason -- "we will not score this" is what NOT_SCORABLE already
    means, whoever decided it.

    There is no member for "the pipeline has not been built yet". A fourth,
    NOT_IMPLEMENTED, existed between phases F and H and was set when a note had
    been fully scored but there was no Decision to publish beside the score.
    Phase H deleted it with the gap it named, and a grep test refuses it back
    into src/ -- a suppression code that means "our fault, not the note's" is
    indistinguishable to the CRM from one that means "this note cannot be
    scored", and the second is the only kind this vocabulary is for.
    """

    NOTE_TOO_SHORT = "note_too_short"
    SYSTEM_EVENT = "system_event"
    UNCLASSIFIABLE = "unclassifiable"
    NOTE_TOO_LONG = "note_too_long"


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class JudgementRequest(BaseModel):
    """The primary route's body: two integers.

    extra="forbid" is load-bearing. The note is ALREADY SAVED in the CRM and is
    fetched by id (Design A); note text is never accepted in THIS body.
    Forbidding extras means a caller that tries to post `note` or `text` gets a
    422 naming the field rather than having it silently ignored -- which would
    leave the caller believing we judged the text they sent.

    The direct route (DECISION[DIRECT_ROUTE], below) is the one exception, and
    it is a SEPARATE model on a separate path: nothing a caller posts here
    starts being read as note text.
    """

    model_config = ConfigDict(extra="forbid")

    # ge=1 on every id (register item 10): 0 or a negative id is a 422
    # invalid_request, never a fetch, a reservation or a counter key.
    lead_id: int = Field(ge=1)
    note_id: int = Field(ge=1)


# The hard ceiling on note text in a request body. A REQUEST-SIZE bound, not a
# rubric one: it is here, in the schema, so an oversized body is a 422 that
# never reaches the pipeline, is never fingerprinted and is never held in a
# judgement. The tenant's own soft limit (TenantConfig.max_note_chars, 2000
# today) is the rubric bound and suppresses with a reason instead.
#
# PROVISIONAL. The real sample's longest note is 340 characters, so both
# numbers are an order of magnitude of headroom above anything observed --
# chosen to be obviously safe rather than measured. See DECISION[DIRECT_ROUTE].
MAX_NOTE_TEXT_CHARS = 4000


class LeadContext(BaseModel):
    """The four lead fields the classifier's context section reads.

    Exactly the four, and nothing else. The fetch route gets a whole `Lead`
    from the backend and uses these four; the direct route is sent these four
    and nothing more, so the CRM cannot widen what reaches a prompt by adding
    fields to its payload. All four are optional because every lead field
    except `id` is nullable in the backend contract (schemas/lead.py).

    extra="forbid": a fifth field is a 422, not a silently ignored one.
    """

    model_config = ConfigDict(extra="forbid")

    leadType: str | None = None
    enquiryType: str | None = None
    project: str | None = None
    status: str | None = None


class _SentNote(BaseModel):
    """The fields every body that carries a saved note shares: the direct
    route's and the history route's. Not a route body itself."""

    model_config = ConfigDict(extra="forbid")

    # ge=1 on every id, as on JudgementRequest (register item 10).
    lead_id: int = Field(ge=1)
    note_id: int = Field(ge=1)
    author_id: int = Field(ge=1)
    note_text: str = Field(max_length=MAX_NOTE_TEXT_CHARS)
    lead: LeadContext


class DirectJudgementRequest(_SentNote):
    """The direct route's body: the saved note, sent by the CRM after the save.

    DECISION[DIRECT_ROUTE] -- the ONE exception to "note text is never accepted
    in a request body", admitted for this route only because the CRM's read
    surface has been unavailable for six weeks. The fetch route is still the
    contract; see ASSUMPTIONS.md.

    `author_id` is the CRM's STORED author for the note, trusted as such. The
    route reaches the service chain only, so there is no user `sub` beside it:
    the scope's subject IS this author, `author:<id>` (register item 92).

    NO SCORE-SHAPED FIELD, and there never may be one. This is a request body
    the CRM controls, so a `band`, a `total`, a `score` or a `mark` here would
    be a caller handing us the answer -- the same rule the model-output schemas
    are held to, for the same reason, and asserted by the same introspection
    test.
    """


class HistoryJudgementRequest(_SentNote):
    """The history route's body (register item 127): an OLD saved note, scored
    so the measures have a past. The direct body plus when the note was
    written; nothing a live judgement's prompt or enforcement reads.

    `note_created_at` must carry an offset -- a naive time is a 422, because a
    guess about which zone it was written in moves the note to another day.
    """

    note_created_at: AwareDatetime


# ---------------------------------------------------------------------------
# Model output -- untrusted, validated through core/validation.py
# ---------------------------------------------------------------------------

# 300 chars is a clarification question, not a paragraph. The cap is a
# structural bound on what we will relay to a salesperson, enforced here so
# an over-long prompt is a validation failure (and gets one reprompt) rather
# than something the CRM has to truncate.
ClarificationPrompt = Annotated[str, StringConstraints(min_length=1, max_length=300)]


class ClassificationOutput(BaseModel):
    """Pass 1: what kind of note is this. One field, by design -- a classifier
    that also volunteers a score is answering a question we did not ask."""

    model_config = ConfigDict(extra="forbid")

    note_type: ClassifierOutput


class VagueOutput(BaseModel):
    """Pass 2: is the note vague, and what would we ask about it.

    The cross-field rule below is a BICONDITIONAL, checked in both directions:

        is_vague     <-> missing_components non-empty AND a prompt is present
        not is_vague <-> missing_components empty     AND prompt is None

    Both halves matter. A model that says "vague" but names nothing gives the
    decision step nothing to act on; one that says "not vague" while filling in
    a prompt would have that prompt sent to a salesperson about a note we just
    judged fine. Either shape is malformed output -- it earns the single
    reprompt, not a repair in code.
    """

    model_config = ConfigDict(extra="forbid")

    is_vague: bool
    missing_components: list[MissingComponent]
    clarification_prompt: ClarificationPrompt | None = None
    reasoning: str

    @model_validator(mode="after")
    def _check_vagueness_is_self_consistent(self) -> VagueOutput:
        # Fixed-vocabulary messages: this runs on model output, and pydantic's
        # message reaches the OutputValidationError's error TYPE only, but the
        # rule in log_safety.py is that our own messages never interpolate
        # content. These name fields, never values.
        has_components = bool(self.missing_components)
        has_prompt = self.clarification_prompt is not None
        if self.is_vague and not (has_components and has_prompt):
            raise ValueError(
                "vague output requires missing_components and clarification_prompt"
            )
        if not self.is_vague and (has_components or has_prompt):
            raise ValueError(
                "non-vague output must have empty missing_components and no "
                "clarification_prompt"
            )
        return self


class ScoreOutput(BaseModel):
    """Pass 3: one yes or no per check.

    The model answers facts; it does not mark and it does not total.
    Which checks are asked is decided per request from the tenant's
    config and the note's type, so a suppressed component's checks are
    never sent and never returned. That the returned set matches the
    asked set is checked in the scoring code, not here: this schema has
    no context channel, so the check is in scoring.py.

    No `total` and no `band`, for the same reason as before.
    """

    model_config = ConfigDict(extra="forbid")

    checks: dict[CheckName, StrictBool]
    reasoning: str


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class NoteAnalysis(BaseModel):
    """The analysis half of a judgement. On a suppressed judgement every field
    is null/empty except note_type, which carries the classifier's answer when
    there was one (including "unclassifiable") and null when we stopped before
    classifying."""

    note_type: ClassifierOutput | None = None
    is_vague: bool | None = None
    missing_components: list[MissingComponent] = Field(default_factory=list)
    clarification_prompt: str | None = None
    reasoning: str | None = None


class ScoreComponent(BaseModel):
    """One row of the score breakdown.

    A suppressed component has `mark: null` and `suppressed: true`, and its
    weight has ALREADY been removed from the denominator. It is not a zero:
    scoring a component 0 and dropping it from the denominator are different
    outcomes, and collapsing them is how a rubric silently penalises note types
    that legitimately cannot answer a component.
    """

    name: ComponentName
    mark: int | None
    weight: int
    suppressed: bool


class NoteScore(BaseModel):
    """The computed score. Every field here is derived in code from marks plus
    TenantConfig; none is accepted from a model."""

    total: int
    band: Band
    denominator: int
    components: list[ScoreComponent]


class Suppressed(BaseModel):
    """Why this judgement carries no score.

    `clarification_prompt` and `prompt_withheld` (register item 64) are for the
    ONE suppression that still asks something: a note below the length floor
    carries a fixed question, no model involved, since there is nothing to
    classify or score. Both are null for every other suppression -- a
    too-long note and a classifier suppression (system_event, unclassifiable)
    have no question to ask at all.
    """

    reason: SuppressedReason
    detail_code: SuppressedDetail
    clarification_prompt: str | None = None
    prompt_withheld: PromptWithheld | None = None


class Decision(BaseModel):
    """What we advise, and what actually happened to the clarification prompt.

    NOTHING HERE COMES FROM A MODEL. Every field is computed by decide() from
    the total, the tenant's thresholds and two counters read out of db2 -- there
    is no field a model output could populate, which is why no output schema
    needs to be checked for one.

    `attempt` is the count AFTER this request: a judgement that sent a prompt
    reports the attempt it just spent, and one that withheld reports the count
    unchanged. `attempts_remaining` is clamped at 0 rather than going negative,
    so a cap that is lowered while counters are live reads as "none left"
    instead of "minus one".
    """

    action: DecisionAction
    prompt_sent: bool
    prompt_withheld: PromptWithheld | None = None
    attempt: int
    attempts_remaining: int

    # Register item 33, the resubmission reference. The hex SHA-256 of the note
    # as it was FIRST prompted on, when db2 still holds that note's attempt
    # state; null otherwise, and null on the primary route always -- there, this
    # request IS the first prompt or there was none.
    #
    # It exists so the CRM can link a resubmission to the judgement it followed
    # WITHOUT this service holding history: we keep one digest for the life of
    # an attempt counter, they keep the judgement. A fingerprint and never note
    # text -- the CRM already has the text, and we do not store it anywhere.
    original_note_fingerprint: str | None = None


class Enforcement(BaseModel):
    """What the CRM should do, on EVERY judgement -- scored or suppressed.

    The point of the block is that the CRM never infers. Before it, a caller
    read `decision.action` and decided for itself what a flag was; a suppressed
    judgement carried no action at all, so it decided from nothing. Here the
    mode in force and the verdict are both stated, and the field is required:
    there is no judgement without one, so an absent block is a bug and never a
    quiet allow.

    `mode` is the TENANT'S, read off TenantConfig at judgement time and
    recorded here rather than looked up later -- a tenant that moves from
    advisory to strict must not change what an old judgement meant.

    `applies_to` is what the verdict is about, and it is null exactly when the
    verdict is `allow`: nothing is flagged, so there is nothing for the flag to
    be about. It is NOT the note id -- it says which THING is being judged, and
    the blocking work adds the second member.

    Derived in decide.py::enforcement from the mode and the action alone. No
    model output reaches it and no request field does.
    """

    mode: EnforcementMode
    verdict: EnforcementVerdict
    applies_to: EnforcementTarget | None = None


class Versions(BaseModel):
    """Stamped on EVERY judgement, scored or suppressed.

    Four independently movable things: the rubric, the prompt set, the model
    the provider reports it ran (LLMResponse.model, not what config asked for),
    and the tenant config. A judgement is only comparable with another that
    carries the same four -- which is what makes it safe to change a weight or
    a prompt without rescoring history.
    """

    rubric_version: str
    prompt_version: str
    model_version: str
    config_version: str


class Judgement(BaseModel):
    """The 200 body, scored or suppressed.

    Exactly one of `score`/`decision` and `suppressed` is populated: a scored
    judgement has score + decision with suppressed null; a suppressed one has
    suppressed set with score and decision null.

    `enforcement` and `versions` are the two blocks that are on BOTH. Which is
    the whole reason `enforcement` has no default: a caller reads one field to
    know what to do, on a judgement of either shape, and never reconstructs it
    from the branch it happens to be looking at.
    """

    note_id: int
    lead_id: int
    author_id: int
    analysis: NoteAnalysis
    score: NoteScore | None = None
    decision: Decision | None = None
    suppressed: Suppressed | None = None
    enforcement: Enforcement
    versions: Versions
    request_id: str
