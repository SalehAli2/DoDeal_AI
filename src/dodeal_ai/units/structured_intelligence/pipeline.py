"""judge_note — the Unit A pipeline, in the one order it may run in.

TWO ENTRY POINTS, ONE PIPELINE. `judge_note` FETCHES the note by id (Design A,
the contract route). `judge_note_direct` is handed the note in the request body
by the CRM, which has just saved it (DECISION[DIRECT_ROUTE]). They differ in
steps 1 and 2 and in nothing else: both join `_judge` at step 3 and every step
from there down is one shared function, so the two routes cannot drift into
judging the same text differently.

The order is not incidental; each step is placed where it is because of what
the step after it costs:

  1. fetch the lead                      404 lead_not_found     (see H2 below)
  2. fetch page one of its notes, match  404 note_not_found
     (the direct route skips 1 and 2: the CRM sent the note, and no
      LeadsClient call is made at all)
  3. too thin, or too long?              -> suppressed, STOP. No reservation,
                                            no model call, nothing spent.
  4. reserve idempotency                 409 duplicate / 503 unavailable
  5. read the rate limit                 (fail open)
  6. read the attempt count              (fail open)
  7. SEAM[STEP3] token pre-flight        (no-op today)
  8. classify, THEN vague + score        three passes, two round-trips
  9. compute, decide                     in code, never from the model
 10. increment counters ONLY if a prompt was actually sent

A PASS IS NOT A CALL. Each of the three passes is one validated model call, and
a malformed answer buys that pass one reprompt (llm_call.call_model) -- so the
happy path is three calls and a judgement that reprompted once costs four. The
pipeline counts passes because passes are what it can see; `reprompt_issued`
names the pass that needed a second attempt.

WHY 8 IS "classify, THEN the other two" (register item 14). Classification must
finish first: the vague template is chosen by type and the applicable components
are chosen by type, so neither of the other two prompts can even be ASSEMBLED
until the answer is in. Vague detection and scoring, on the other hand, do not
depend on each other at all -- so they are issued together with asyncio.gather
and the happy path costs two round-trips, not three, while the happy-path call
count stays three. `return_exceptions=False`: the first failure propagates
through the release-on-error path below and the key is released exactly once --
and because each pass owns its own reprompt, a reprompt on one of the two
re-issues that one alone.

WHY THE COUNTERS MOVE LAST, AND ONLY SOMETIMES. Step 10 runs after the
judgement exists and outside the release-on-error block, because by then the
request has been answered: the note was fetched, three calls were paid for, and
a decision was made. An unreachable db2 at that moment must not undo any of
that, so the increments fail open (state.py) AND sit where a raise could not
reach the release path. And they move only when a prompt was ACTUALLY SENT --
a counter that advanced on a withheld prompt would rate-limit a salesperson for
questions they never received.

WHY THIS ORDER, at the two places it matters:

  The length gate BEFORE the reservation. A three-word note -- or one longer
  than a tenant will score as a single interaction -- is refused without
  reserving anything, so the salesperson can fix it and resubmit immediately
  rather than being told 409 for the next 24 hours.

  The reservation BEFORE any model call. It is the only thing standing between
  a double-submit and paying twice, so it must be claimed before the first
  thing that costs money.

RELEASE ON EVERY NON-200 AFTER RESERVING. If we reserved and then failed for a
reason of OURS -- the backend went down, the model went down, output would not
validate -- the caller must be able to retry. Without the release they would
get 409 for a judgement that never happened.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.cost.limiter import token_preflight
from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    NoteNotFoundError,
)
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.schemas.lead import Lead, LeadNote
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.tools.leads import LeadsClient
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import (
    CLASSIFY_LABEL,
    classify,
    suppression_for,
)
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.decide import decide
from dodeal_ai.units.structured_intelligence.schemas import (
    Decision,
    DirectJudgementRequest,
    Judgement,
    JudgementRequest,
    NoteAnalysis,
    NoteType,
    Suppressed,
    SuppressedDetail,
    SuppressedReason,
    Versions,
)
from dodeal_ai.units.structured_intelligence.scoring import (
    SCORE_LABEL,
    compute_score,
    score_note,
)
from dodeal_ai.units.structured_intelligence.vague import VAGUE_LABEL, detect_vagueness

_logger = logging.getLogger("dodeal_ai.unit_a")

# Version stamps. RUBRIC and PROMPT_SET move when the rubric or the prompt
# files change; both are stamped on EVERY judgement, scored or suppressed, so
# two judgements are only ever compared when all four stamps match.
RUBRIC_VERSION = "note_rubric_v1"
PROMPT_SET_VERSION = "unit_a_prompts_v1"

# No model ran, so there is nothing the provider reported. Deliberately NOT
# settings.llm_model: core/llm/client.py is explicit that `model` is what the
# provider REPORTS ran, not what config asked for, and stamping the configured
# pin on a judgement no model touched would make an unspent judgement look
# like a spent one.
NO_MODEL = ""


@dataclass(frozen=True, slots=True)
class JudgementDeps:
    """Everything judge_note reaches the outside world through.

    Passed in rather than constructed here, so a test supplies fakes without
    patching a module global. The three OPERATIONAL state functions are NOT on
    this dataclass -- they are called through the `state` module, which tests
    redirect with monkeypatch.setattr(state, "get_operational_client", ...).
    Putting them here too would give the same thing two injection points.

    `llm` is the client itself, not a factory: get_llm_client() is the FastAPI
    dependency and has already been resolved (and, in tests, overridden) by the
    time the route calls this.

    `settings` is here rather than read through get_settings() inside the model
    call, because llm_timeout_seconds is a per-call argument and a test that
    wants a different one should set it on the deps it already builds, not reach
    into a cache the whole process shares.

    `leads` is None on the DIRECT route (DECISION[DIRECT_ROUTE]), which is
    handed the note and fetches nothing. None rather than a stand-in client:
    "there is no client here" is the true statement, and only `_fetch_note`
    reads the field -- which the direct entry point never reaches.
    """

    leads: LeadsClient | None
    llm: LLMClient
    config: TenantConfig
    settings: Settings


# Either request shape. Both carry lead_id and note_id, which is all the shared
# pipeline reads off a request -- the direct body's three extra fields are
# unpacked into a Lead and a LeadNote by judge_note_direct before the shared
# function is entered, so nothing below this line knows which route it is on.
_JudgementInput = JudgementRequest | DirectJudgementRequest


def _versions(config: TenantConfig, model_version: str = NO_MODEL) -> Versions:
    return Versions(
        rubric_version=RUBRIC_VERSION,
        prompt_version=PROMPT_SET_VERSION,
        model_version=model_version,
        config_version=config.config_version,
    )


def _suppressed(
    scope: TenantScope,
    request: _JudgementInput,
    *,
    author_id: int,
    reason: SuppressedReason,
    detail: SuppressedDetail,
    config: TenantConfig,
    analysis: NoteAnalysis | None = None,
    model_version: str = NO_MODEL,
) -> Judgement:
    """Build a suppressed judgement: score and decision are null, never zero.

    A suppressed note has no score at all. It is not a 0, not a `poor` band and
    not an absent field that something downstream will read as 0 -- so it
    cannot be averaged into a salesperson's figures.

    `model_version` separates the two kinds of suppression on the judgement
    itself: a thin note was refused before anything was spent and carries "",
    while a system_event was refused BY a classifier and carries what that
    classifier reported. Both are suppressed; only one cost money, and the
    stamp is the only place that difference survives.
    """
    return Judgement(
        note_id=request.note_id,
        lead_id=request.lead_id,
        author_id=author_id,
        analysis=analysis or NoteAnalysis(),
        score=None,
        decision=None,
        suppressed=Suppressed(reason=reason, detail_code=detail),
        versions=_versions(config, model_version),
        request_id=scope.request_id,
    )


def _length_gate(
    note: LeadNote, config: TenantConfig
) -> tuple[SuppressedReason, SuppressedDetail] | None:
    """The structural bounds on the note itself: too little, or too much.

    Returns the (reason, detail) pair to suppress with, or None to carry on.
    ONE function and ONE call site, because both bounds are the same kind of
    check -- counted on the stripped text, decided before any reservation and
    before anything is spent -- and a second gate somewhere else is how the two
    ends of the range start disagreeing about what "the text" is.

    TOO LITTLE -> insufficient_evidence / note_too_short. Below the character
    floor OR the token floor: "ok" fails on length, and a long string of one
    repeated word fails on tokens. Whitespace-split is deliberately crude -- it
    is a floor for "is there anything here at all", not a linguistic measure,
    and it behaves the same for Arabic, English and mixed text (one prompt set,
    no language branch).

    TOO MUCH -> not_scorable / note_too_long. Above config.max_note_chars. Not
    insufficient_evidence: the trouble is not that there is too little to judge
    but that there is too much to judge as ONE interaction, which is the grain
    the rubric scores at. It applies to BOTH routes -- a note too long to be one
    interaction is too long whichever way its text reached us -- and it is a
    suppressed 200 rather than a 422, because the note IS saved in the CRM and
    refusing to score it is an answer about the note, not about the request.
    """
    text = note.note.strip()
    if len(text) < config.min_note_chars or len(text.split()) < config.min_note_tokens:
        return (SuppressedReason.INSUFFICIENT_EVIDENCE, SuppressedDetail.NOTE_TOO_SHORT)
    if len(text) > config.max_note_chars:
        return (SuppressedReason.NOT_SCORABLE, SuppressedDetail.NOTE_TOO_LONG)
    return None


async def _fetch_note(
    scope: TenantScope, request: JudgementRequest, deps: JudgementDeps
) -> tuple[Lead, LeadNote]:
    """Fetch the lead, then find the note on page one of its notes.

    Both come back. The lead is not fetched only to prove it exists: four of its
    fields are the classifier's context section, and the alternative -- fetching
    it twice, or passing the id and letting the classifier fetch -- would make
    the number of backend calls depend on the note type.

    ASSUMPTION[Q8]: the target note is on PAGE ONE (notes come back newest
    first, 25 per page), so one un-paged fetch finds it. Nothing branches on
    this -- there is no paging code to take a second path -- and if a note can
    fall off page one, the fix is query parameters in tools/leads.py at step 4,
    not a change here.

    Design A: the note is matched BY ID, never taken by position. "The newest
    note" would judge whatever arrived most recently, which on a busy lead is
    not the note the caller asked about.

    H2 CAVEAT: the watchdog collapses every backend failure into
    ExternalCallError, so a genuine 404 for a missing lead is indistinguishable
    here from a 500 or a timeout, and both become 503 backend_unavailable. That
    is why LeadNotFoundError is not raised from this function today -- inferring
    "not found" from ExternalCallError would report a backend outage to the CRM
    as a missing lead. Typed backend errors are step 4 (audit H2); when they
    land, the 404 branch goes here and nothing else moves.
    """
    # Only judge_note reaches this function, and only the fetch route reaches
    # judge_note -- so a None client here would mean the direct entry point had
    # grown a fetch, which is the one thing this route may not do.
    assert deps.leads is not None
    try:
        lead = await deps.leads.get_lead(scope, request.lead_id)
        notes = await deps.leads.get_lead_notes(scope, request.lead_id)
    except (ExternalCallError, BackendKeyError) as exc:
        # The real reason is already in the log: the watchdog logged the failure
        # type, and BackendKeyError logged its fixed reason code. Nothing about
        # either is repeated to the caller.
        _logger.warning(
            "judgement_backend_unavailable",
            extra={
                "reason_code": "backend_unavailable",
                "tenant": scope.tenant,
                "request_id": scope.request_id,
                "error_type": type(exc).__name__,
            },
        )
        raise BackendUnavailableError() from None

    for note in notes:
        if note.id == request.note_id:
            return lead, note

    raise NoteNotFoundError()


async def judge_note(
    scope: TenantScope,
    request: JudgementRequest,
    *,
    resubmission: bool,
    deps: JudgementDeps,
) -> Judgement:
    """Judge one already-saved note, FETCHED by id. The contract route.

    See the module docstring for the order. This entry point owns steps 1 and 2
    -- the lead and the note come from the backend -- and then hands over to
    `_judge`, which is every step from the length gate down and is shared with
    judge_note_direct.

    ASSUMPTION[Q1]: the route this runs behind depends on gate4_cost, i.e. the
    CRM forwards the END USER's JWT and we run Gate 1 (auth) -> Gate 2
    (tenancy) -> Gate 4 (cost) exactly as the probe route does. If the CRM
    turns out to call us as a service instead, the principal source swaps
    behind D1's seam -- this function already takes a TenantScope, not a
    RequestContext, so it does not change; only what builds the scope does, and
    subjects from a service principal get labelled asserted.

    `resubmission` changes what happens to the clarification prompt, never what
    is fetched or scored: the note goes through the same three passes and the
    same arithmetic, attempts are READ and never incremented, and any prompt is
    withheld with prompt_withheld="resubmission". It is also the only route that
    reads back the resubmission reference (register item 33) -- and even there
    it is the FINGERPRINT that decides 200 versus 409, exactly as on the primary
    route. An edited note is a new fingerprint and so a new judgement; an
    unedited one is a duplicate whichever route it arrives on.
    """
    lead, note = await _fetch_note(scope, request, deps)
    return await _judge(
        scope,
        request,
        lead,
        note,
        author_id=note.author_id,
        resubmission=resubmission,
        deps=deps,
    )


async def judge_note_direct(
    scope: TenantScope,
    request: DirectJudgementRequest,
    *,
    resubmission: bool,
    deps: JudgementDeps,
) -> Judgement:
    """Judge a note the CRM SENT US, having just saved it. DECISION[DIRECT_ROUTE].

    The one exception to "note text is never accepted in a request body",
    admitted for this entry point only, because the CRM's read surface has been
    unavailable for six weeks and it can send the saved note server-side after
    the save. The fetch route above is still the contract and is unchanged.

    THE ONLY DIFFERENCE IS WHERE THE TEXT CAME FROM. There is no fetch, so
    steps 1 and 2 do not run and no LeadsClient call is made; everything from
    the length gate down is the same function, in the same order, with the same
    reservation, the same counters and the same logging. A judgement made here
    and one made by judge_note over the same text are identical but for the
    request id.

    CREDENTIAL: the CRM forwards the note author's own user JWT, so this route
    sits behind exactly the gate chain the fetch route does, and the per-user
    cost cap and the clarification rate limit key on `sub` as built.
    `request.author_id` is the CRM's STORED author and is trusted as that; it is
    NOT checked against `sub`, and a difference never rejects the request -- it
    is ASSUMPTION[Q7]'s two id spaces, and refusing on it would refuse every
    judgement the moment the CRM's ids and the token's ids stop coinciding. The
    difference is recorded on the outcome line (ids only, never text) so that
    "the CRM is sending someone else's JWT" is visible rather than inferred.
    """
    lead = Lead(
        id=request.lead_id,
        leadType=request.lead.leadType,
        enquiryType=request.lead.enquiryType,
        project=request.lead.project,
        status=request.lead.status,
    )
    note = LeadNote(
        id=request.note_id,
        note=request.note_text,
        author=None,
        author_id=request.author_id,
        # Nothing reads createdAt today -- not the prompts, not the scoring, not
        # the counters -- and the CRM is not asked for it, because a timestamp
        # we do not use is a field that can be wrong for free. Register item 32
        # (aware datetime parsing) MUST guard this empty string: the day
        # createdAt becomes a parsed datetime, a note that arrived on this route
        # has no value to parse, and a parser that assumes one will raise on the
        # direct route only.
        createdAt="",
    )
    return await _judge(
        scope,
        request,
        lead,
        note,
        author_id=request.author_id,
        resubmission=resubmission,
        deps=deps,
        author_differs_from_subject=str(request.author_id) != scope.subject,
    )


async def _judge(
    scope: TenantScope,
    request: _JudgementInput,
    lead: Lead,
    note: LeadNote,
    *,
    author_id: int,
    resubmission: bool,
    deps: JudgementDeps,
    author_differs_from_subject: bool | None = None,
) -> Judgement:
    """Every step from the length gate down, for both entry points.

    ONE body, not two. The two routes differ only in how the Lead and the
    LeadNote were obtained; everything that costs money, everything that
    reserves, everything that decides and everything that is logged is here, so
    the two routes cannot drift into judging the same text differently.

    `author_differs_from_subject` is None on the fetch route -- there is no
    claimed author to compare, the backend's is the only one -- and a bool on
    the direct route, where the body carries one. It reaches the outcome line
    and nothing else: it is not a gate, not a rejection and not an input to any
    decision.
    """
    config = deps.config

    # --- length: before any reservation, before any spend -------------------
    gated = _length_gate(note, config)
    if gated is not None:
        gate_reason, gate_detail = gated
        judgement = _suppressed(
            scope,
            request,
            author_id=author_id,
            reason=gate_reason,
            detail=gate_detail,
            config=config,
        )
        _log_outcome(
            scope,
            judgement,
            model_passes=0,
            author_differs_from_subject=author_differs_from_subject,
        )
        return judgement

    # --- reserve: the only thing between a double-submit and paying twice ---
    fingerprint = state.note_fingerprint(note.note)
    try:
        claimed = await state.reserve_idempotency(
            scope.tenant,
            request.note_id,
            fingerprint,
            ttl=config.idempotency_ttl_seconds,
            request_id=scope.request_id,
        )
    except state.IdempotencyUnavailableError:
        raise IdempotencyUnavailableResponse() from None
    if not claimed:
        raise DuplicateRequestError()

    decision: Decision | None = None
    try:
        # ASSUMPTION[Q7]: the rate limit is keyed on scope.subject -- who is
        # ASKING -- not on note.author_id, who WROTE the note. The limit exists
        # to stop us pestering one person, and the person we would pester is
        # the one making the request. Both reads fail OPEN (0), so a db2 blip
        # cannot 503 a judgement that is otherwise fine.
        #
        # Read HERE, before anything is spent, and carried down to decide():
        # re-reading them after the model calls would let a counter moved by a
        # concurrent request change this judgement's answer halfway through.
        rate_count = await state.read_rate_limit(
            scope.tenant, scope.subject, request_id=scope.request_id
        )
        attempts = await state.read_attempts(
            scope.tenant,
            request.lead_id,
            request.note_id,
            request_id=scope.request_id,
        )

        # SEAM[STEP3]: the token budget pre-flight. A no-op that logs once per
        # process; step 3 replaces the body, not this call site.
        await token_preflight(scope)

        # --- classify: the first thing that costs money ---------------------
        classification, classify_response = await classify(
            deps.llm, note, lead, settings=deps.settings
        )
        model_passes = 1

        detail = suppression_for(classification.note_type)
        if detail is not None:
            judgement = _suppressed(
                scope,
                request,
                author_id=author_id,
                reason=SuppressedReason.NOT_SCORABLE,
                detail=detail,
                config=config,
                analysis=NoteAnalysis(note_type=classification.note_type),
                model_version=classify_response.model,
            )
        else:
            # suppression_for returned None, so this is one of the six scored
            # types -- narrowed here because ClassifierOutput also admits the
            # "unclassifiable" string, which stopped above.
            note_type = classification.note_type
            assert isinstance(note_type, NoteType)

            # --- vague + score: issued together, awaited together ------------
            vague_result, score_result = await asyncio.gather(
                detect_vagueness(
                    deps.llm,
                    note,
                    note_type,
                    config=config,
                    settings=deps.settings,
                ),
                score_note(
                    deps.llm,
                    note,
                    note_type,
                    config=config,
                    settings=deps.settings,
                ),
            )
            model_passes = 3

            vague_output, vague_response = vague_result
            score_output, score_response = score_result
            analysis = NoteAnalysis(
                note_type=note_type,
                is_vague=vague_output.is_vague,
                missing_components=vague_output.missing_components,
                clarification_prompt=vague_output.clarification_prompt,
                reasoning=vague_output.reasoning,
            )
            score = compute_score(score_output.marks, note_type, config)
            decision = decide(
                score,
                analysis,
                attempts=attempts,
                rate_count=rate_count,
                config=config,
                resubmission=resubmission,
            )
            if resubmission:
                # Read only here: the reference belongs to a Decision, and a
                # judgement that suppressed or failed has none to hang it on --
                # so the primary route and every stop before this point spend no
                # db2 read on it. decide()'s signature is fixed and takes no
                # store, which is right: this is a REFERENCE the CRM links on,
                # not an input to the decision.
                decision = decision.model_copy(
                    update={
                        "original_note_fingerprint": (
                            await state.read_attempt_fingerprint(
                                scope.tenant,
                                request.lead_id,
                                request.note_id,
                                request_id=scope.request_id,
                            )
                        )
                    }
                )

            _check_one_model_answered(
                scope,
                (
                    (CLASSIFY_LABEL, classify_response),
                    (VAGUE_LABEL, vague_response),
                    (SCORE_LABEL, score_response),
                ),
            )
            judgement = Judgement(
                note_id=request.note_id,
                lead_id=request.lead_id,
                author_id=author_id,
                analysis=analysis,
                score=score,
                decision=decision,
                suppressed=None,
                # The SCORING pass's model, not the classifier's: the marks are
                # what the judgement is, and on a reprompted pass it is the
                # second response -- the call the marks actually came from.
                versions=_versions(config, score_response.model),
                request_id=scope.request_id,
            )
    except Exception:
        # Reserved, then failed: release so the caller can retry instead of
        # being told 409 for a judgement that never happened.
        await state.release_idempotency(
            scope.tenant, request.note_id, fingerprint, request_id=scope.request_id
        )
        raise

    # --- step 10: the counters, only for a prompt that is actually sent -----
    #
    # OUTSIDE the try, deliberately. The judgement exists: the note was
    # fetched, three calls were paid for, a decision was made. A db2 outage now
    # must not release the reservation and turn an answered request into a 503,
    # so these calls are placed where a raise could not reach the release path
    # -- as well as failing open inside state.py. Both guards, because one of
    # them is a promise another module keeps.
    #
    # decision is None on every suppressed judgement, so a suppressed note
    # never moves a counter -- and neither does a resubmission, which withholds
    # its prompt by policy and so never reaches prompt_sent.
    if decision is not None and decision.prompt_sent:
        await state.increment_rate_limit(
            scope.tenant,
            scope.subject,
            ttl=config.rate_limit_window_seconds,
            request_id=scope.request_id,
        )
        await state.increment_attempts(
            scope.tenant,
            request.lead_id,
            request.note_id,
            ttl=config.attempt_ttl_seconds,
            request_id=scope.request_id,
        )
        # Register item 33: the note we are prompting on, recorded beside the
        # counter it belongs to, at the one moment prompt_sent becomes true.
        await state.write_attempt_fingerprint(
            scope.tenant,
            request.lead_id,
            request.note_id,
            fingerprint,
            ttl=config.attempt_ttl_seconds,
            request_id=scope.request_id,
        )

    _log_outcome(
        scope,
        judgement,
        model_passes=model_passes,
        author_differs_from_subject=author_differs_from_subject,
    )
    return judgement


def _check_one_model_answered(
    scope: TenantScope, responses: tuple[tuple[str, LLMResponse], ...]
) -> None:
    """Log when the three passes did not all come back from the same model.

    `model_version` stamps ONE string, and the judgement is stamped with
    scoring's -- so if the provider rolled a model between the classify call
    and the score call, the stamp is true for the marks and quietly not true
    for the classification. That is a real thing to know about a deployment and
    an invisible one otherwise, so it gets a line.

    THE LABELS ONLY. The pass labels are a fixed vocabulary (`llm.unit_a.*`);
    the note is not in scope here and never could be. It is a WARNING rather
    than a failure because a mixed judgement is not wrong -- every pass
    validated -- it is merely stamped less precisely than the field suggests.
    """
    if len({response.model for _, response in responses}) == 1:
        return
    _logger.warning(
        "model_version_mismatch",
        extra={
            "reason_code": "model_version_mismatch",
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "labels": ",".join(label for label, _ in responses),
        },
    )


def _log_outcome(
    scope: TenantScope,
    judgement: Judgement,
    *,
    model_passes: int,
    author_differs_from_subject: bool | None = None,
) -> None:
    """One structured line per judgement.

    Fields only, via extra=, and every one of them is an identifier or a member
    of a fixed vocabulary. NEVER the note text, never the reasoning, never the
    clarification prompt -- the whole point of the unit is that it reads note
    bodies, so this is the log line most likely to leak one.

    `model_passes` counts PASSES, not calls, and the field is named for what it
    counts. A pass that reprompted cost two calls, and the pipeline cannot see
    that: the reprompt lives inside `call_model`, which is the point -- one
    place owns the untrusted parse and its one recovery. The line that says a
    pass needed two calls is `reprompt_issued`, which carries the label of the
    pass it was issued for. A field called `model_calls` that could be short by
    up to three would be worse than one that says what it means.

    THE SCORE IS READ OFF THE JUDGEMENT, not passed alongside it. Between
    phases F and H this function took a `score=` parameter, because the
    arithmetic ran but the judgement could not yet carry it; `decide()` closed
    that gap, so the parameter went with it. Two sources for one number is how
    a log line starts disagreeing with the response it describes.

    `prompt_withheld` is on the line and not only in the response. It is the
    difference between "we asked" and "we chose not to", and the alert worth
    having is on a WITHHELD reason appearing far more often than expected --
    a rate limit set too low reads, in the response alone, as a service that
    simply asks fewer questions.

    `author_differs_from_subject` appears on DIRECT-ROUTE lines only, where the
    body carries a claimed author to compare with the token's subject; it is
    omitted entirely on the fetch route, which has nothing to compare. A bool,
    never the two ids and never anything derived from the note -- it answers
    "is the CRM sending us someone else's JWT, or are these simply two id
    spaces (ASSUMPTION[Q7])?", which is a question about a deployment and not
    about a note. It is recorded, never enforced: no request is refused for it.
    """
    # Present only when there is something to say: the fetch route passes None
    # and the key never reaches the line at all, so a collector filtering on it
    # sees direct-route judgements and nothing else.
    author_field = (
        {}
        if author_differs_from_subject is None
        else {"author_differs_from_subject": author_differs_from_subject}
    )

    if judgement.suppressed is not None:
        _logger.info(
            "judgement_suppressed",
            extra={
                "tenant": scope.tenant,
                "request_id": scope.request_id,
                "note_type": judgement.analysis.note_type,
                "suppressed_reason": judgement.suppressed.reason.value,
                "suppressed_detail": judgement.suppressed.detail_code.value,
                "model_passes": model_passes,
                **author_field,
            },
        )
        return

    assert judgement.score is not None and judgement.decision is not None
    withheld = judgement.decision.prompt_withheld
    _logger.info(
        "judgement_completed",
        extra={
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "note_type": judgement.analysis.note_type,
            "band": judgement.score.band.value,
            "denominator": judgement.score.denominator,
            "action": judgement.decision.action.value,
            "prompt_sent": judgement.decision.prompt_sent,
            "prompt_withheld": withheld.value if withheld is not None else None,
            "attempt": judgement.decision.attempt,
            "model_passes": model_passes,
            **author_field,
        },
    )
