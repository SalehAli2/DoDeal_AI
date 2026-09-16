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
     (1 and 2 start together, register item 9; the lead's error wins)
     (the direct route skips 1 and 2: the CRM sent the note, and no
      LeadsClient call is made at all)
  3. too thin, or too long?              -> suppressed, STOP. No reservation,
                                            no model call, nothing spent.
  4. reserve idempotency, SHORT          409 duplicate / 503 unavailable
  5. read the attempt count              (fail open; provisional, see step 8)
  6. token pre-flight                    429 token_budget_exceeded
  7. classify, THEN vague + score        three passes, two round-trips
  8. compute, decide -- and the attempt  ONE db2 trip, and only when it can
     cap and rate limit in one breath    change the answer (fail open)
  9. confirm the reservation, LONG       the judgement exists (fail open);
                                         a classifier suppression confirms too
 10. record the prompted note's          ONLY if a prompt was actually sent
     fingerprint

A PASS IS NOT A CALL. Each of the three passes is one validated model call, and
a malformed answer buys that pass one reprompt (llm_call.call_model) -- so the
happy path is three calls and a judgement that reprompted once costs four. The
pipeline counts passes because passes are what it can see; `reprompt_issued`
names the pass that needed a second attempt.

WHY 7 IS "classify, THEN the other two" (register item 14). Classification must
finish first: the vague template is chosen by type and the applicable components
are chosen by type, so neither of the other two prompts can even be ASSEMBLED
until the answer is in. Vague detection and scoring, on the other hand, do not
depend on each other at all -- so they are issued together with
`gather_or_cancel` (core/resilience.py) and the happy path costs two
round-trips, not three, while the happy-path call count stays three. The first
failure propagates through the release-on-error path below and the key is
released exactly once -- and because each pass owns its own reprompt, a
reprompt on one of the two re-issues that one alone. The helper rather than
`asyncio.gather` because gather leaves the SIBLING running when one pass
raises: the request has already failed, and an abandoned pass whose answer
comes back malformed would spend a reprompt on a judgement nobody will ever
receive (register item 63).

WHY BOTH COUNTERS MOVE AT STEP 8, IN ONE SCRIPT (register items 27 and 119).
Taking a slot is how each guard is checked (state.take_prompt_slots): a
separate check and increment could both see "one under the cap". The step 5
read is only provisional -- two requests for one note both read 0 -- so the
script checks the attempt cap again as it takes, and the request that lost the
race reads back the cap and reports attempt_cap. Both move only when a prompt
would be SENT; a counter that advanced on a withheld prompt would limit a
salesperson for questions they never received. The trip fails open inside
state.py, so it cannot raise into the release path from inside the try.

WHY THIS ORDER, at the two places it matters:

  The length gate BEFORE the reservation. A three-word note -- or one longer
  than a tenant will score as a single interaction -- is refused without
  reserving anything, so the salesperson can fix it and resubmit immediately
  rather than being told 409.

  The reservation BEFORE any model call. It is the only thing standing between
  a double-submit and paying twice, so it must be claimed before the first
  thing that costs money.

RELEASE ON ANY EXIT AFTER RESERVING (register item 82). If we reserved and then
did not produce the judgement -- the backend went down, the model went down,
output would not validate, the deadline passed, the client went away, the worker
is shutting down -- the caller must be able to retry. Without the release they
would get 409 for a judgement that never happened. The last three of those
arrive as a CancelledError, which is not an Exception, so the release runs on
BaseException.

RESERVE SHORT, CONFIRM LONG (register item 82). A worker killed outright runs no
except and no finally, so the release cannot be the only thing that frees a key.
The reservation is taken for IDEMPOTENCY_INFLIGHT_MULTIPLIER deadlines, and only
once the judgement exists is it confirmed for the tenant's long idempotency TTL.
A hard kill leaves a key that expires in minutes, not a note locked for a day.

ONE DEADLINE PER JUDGEMENT (register item 83). The CRM waits inline (Q16), and
the per-call budgets alone sum to minutes: two backend reads, three model
passes, a reprompt each, Redis between. So each entry point runs everything
after its clock starts -- the fetch included -- under one
`judgement_deadline_seconds`, and past it answers 503
judgement_deadline_exceeded. The per-call timeouts are unchanged; the deadline
is the bound on their sum, not a replacement for any of them.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable
from dataclasses import dataclass

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.cost.limiter import token_preflight
from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    JudgementDeadlineExceeded,
    NoteNotFoundError,
)
from dodeal_ai.core.inflight import current_inflight
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.resilience import ExternalCallError, gather_or_cancel
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
    PromptWithheld,
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

# How many judgement deadlines a reservation lives while its judgement runs.
# A hard kill leaves the key behind: four deadlines is long enough that the CRM's
# own retries still meet 409 while the original may be running, and short enough
# that a killed worker does not lock the note for a day. PROVISIONAL, like the
# deadline it multiplies; the load lane sets the two together.
IDEMPOTENCY_INFLIGHT_MULTIPLIER = 4


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


@dataclass(frozen=True, slots=True)
class _Timings:
    """The four durations on an outcome line, in milliseconds (register item 72).

    THE MONOTONIC CLOCK, never the wall clock. `datetime.now()` can go
    BACKWARDS -- an NTP correction mid-request produces a negative duration, and
    a negative duration in a latency dashboard is not a small error, it is a
    number nobody can interpret. `time.monotonic()` is the one clock that only
    counts forwards, and it is deliberately unrelated to any timestamp: these
    fields say how LONG, never when.

    `elapsed_ms` is the whole judgement, from the entry point to the outcome
    line -- so on the fetch route it includes the two backend calls, which is
    the point: those are most of what a slow judgement on that route is.

    The three pass fields are None when the pass DID NOT RUN, and null is the
    honest answer there: a suppressed note that never reached scoring did not
    take zero milliseconds to score, it did not score. Zero would average into
    a latency panel as a fast pass and quietly drag the number down.

    THEY DO NOT ADD UP, and are not meant to. Vague detection and scoring
    OVERLAP -- that is the whole reason they are gathered -- so their sum can
    exceed the elapsed time they ran inside. Anything reading these as a
    breakdown of `elapsed_ms` is reading them wrong.
    """

    elapsed_ms: int
    classify_ms: int | None = None
    vague_ms: int | None = None
    score_ms: int | None = None

    def fields(self) -> dict[str, int | None]:
        """The four, plus the in-flight count read AT OUTCOME TIME.

        `inflight` is read here rather than passed in because the question it
        answers is "what else was this pod doing when this judgement finished" --
        which is a fact about the moment the line is written, not about the
        moment the request started. Numbers only, all five: nothing here is
        derived from a note, and nothing here can be.
        """
        return {
            "elapsed_ms": self.elapsed_ms,
            "classify_ms": self.classify_ms,
            "vague_ms": self.vague_ms,
            "score_ms": self.score_ms,
            "inflight": current_inflight(),
        }


def _ms_since(started: float) -> int:
    """Whole milliseconds since a `time.monotonic()` reading.

    Truncated, not rounded, so a sub-millisecond pass reads 0 rather than 1 --
    and monotonic never runs backwards, so the result is never negative.
    """
    return int((time.monotonic() - started) * 1000)


async def _timed[T](coro: Awaitable[T]) -> tuple[T, int]:
    """Await `coro` and report how long it took.

    The two gathered passes need a clock each, because they run at the same
    time: one pair of readings around the gather would measure the slower of
    them twice and say nothing about the other.
    """
    started = time.monotonic()
    result = await coro
    return result, _ms_since(started)


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
    """Fetch the lead and page one of its notes together, then find the note.

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
        # Register item 9: both reads start together. The lead goes first, so it
        # is the error reported when both have failed, and a failure of either
        # cancels the other rather than leaving a backend call running.
        lead, notes = await gather_or_cancel(
            deps.leads.get_lead(scope, request.lead_id),
            deps.leads.get_lead_notes(scope, request.lead_id),
        )
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
    # The clock starts HERE, not in _judge: the two backend calls below are
    # part of what this route costs a salesperson waiting on an answer, and an
    # elapsed_ms that began after them would be silent about the slowest thing
    # the fetch route does. time.monotonic, never a wall clock -- see _Timings.
    started = time.monotonic()
    # The deadline starts with the clock, so the two backend reads spend it too.
    try:
        async with asyncio.timeout(deps.settings.judgement_deadline_seconds):
            lead, note = await _fetch_note(scope, request, deps)
            return await _judge(
                scope,
                request,
                lead,
                note,
                author_id=note.author_id,
                resubmission=resubmission,
                deps=deps,
                started=started,
            )
    except TimeoutError:
        raise _deadline_exceeded(scope, started) from None


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
    # As on the fetch route, the first statement in the function -- so the two
    # routes' elapsed_ms mean the same thing, minus the fetch this one does not
    # do. Comparing them is how "is the CRM's read surface the slow part?" gets
    # answered when it comes back.
    started = time.monotonic()
    # The same deadline as the fetch route, from the same point, so the two
    # routes' 503s mean the same thing.
    try:
        async with asyncio.timeout(deps.settings.judgement_deadline_seconds):
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
                # Nothing reads createdAt today -- not the prompts, not the
                # scoring, not the counters -- and the CRM is not asked for it,
                # because a timestamp we do not use is a field that can be wrong
                # for free. Register item 32 (aware datetime parsing) MUST guard
                # this empty string: the day createdAt becomes a parsed
                # datetime, a note that arrived on this route has no value to
                # parse, and a parser that assumes one will raise on the direct
                # route only.
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
                started=started,
                author_differs_from_subject=str(request.author_id) != scope.subject,
            )
    except TimeoutError:
        raise _deadline_exceeded(scope, started) from None


def _deadline_exceeded(scope: TenantScope, started: float) -> JudgementDeadlineExceeded:
    """Log the judgement that ran out of time, and hand back the error to raise.

    One line, one shape, for both entry points: ids and two numbers, nothing
    from the note. `elapsed_ms` says how far past the deadline the cancellation
    actually landed, and `inflight` says what else the pod was doing -- the
    two things worth knowing about a request that did not finish.
    """
    _logger.warning(
        "judgement_deadline_exceeded",
        extra={
            "reason_code": "judgement_deadline_exceeded",
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "elapsed_ms": _ms_since(started),
            "inflight": current_inflight(),
        },
    )
    return JudgementDeadlineExceeded()


async def _judge(
    scope: TenantScope,
    request: _JudgementInput,
    lead: Lead,
    note: LeadNote,
    *,
    author_id: int,
    resubmission: bool,
    deps: JudgementDeps,
    started: float,
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

    `started` is the entry point's `time.monotonic()` reading, taken there and
    not here so that the fetch route's two backend calls are inside its
    `elapsed_ms`. It is a DURATION baseline and never a timestamp: nothing
    compares it to a clock, and it is not on any line.
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
            # No pass ran, so all three are null. Not zero: this note did not
            # take no time to classify, it was never classified.
            timings=_Timings(elapsed_ms=_ms_since(started)),
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
            # SHORT: the in-flight lifetime. The long one is the confirm's, at
            # the end of the try below. Rounded UP, never down: a deadline
            # under a quarter-second would otherwise give EX 0, which Redis
            # refuses, and a refused reservation is a 503.
            ttl=math.ceil(
                deps.settings.judgement_deadline_seconds
                * IDEMPOTENCY_INFLIGHT_MULTIPLIER
            ),
            request_id=scope.request_id,
        )
    except state.IdempotencyUnavailableError:
        raise IdempotencyUnavailableResponse() from None
    if not claimed:
        raise DuplicateRequestError()

    decision: Decision | None = None
    # None until the pass that fills each one actually runs, so a judgement that
    # stopped early carries null rather than a made-up zero.
    classify_ms: int | None = None
    vague_ms: int | None = None
    score_ms: int | None = None
    try:
        # Read HERE, before anything is spent: it picks step 8's trip, and that
        # trip's script re-checks the cap as it takes, so a concurrent request
        # for this note cannot also send. Fails OPEN (0).
        attempts = await state.read_attempts(
            scope.tenant,
            request.note_id,
            request_id=scope.request_id,
        )

        # The token budget, read before the first thing that costs money. Over
        # budget is 429 token_budget_exceeded and releases the reservation
        # below like any other non-200 after reserving.
        await token_preflight(scope)

        # --- classify: the first thing that costs money ---------------------
        (classification, classify_response), classify_ms = await _timed(
            classify(deps.llm, note, lead, scope=scope, settings=deps.settings)
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
            # A clock EACH, because these two run at the same time: one pair
            # of readings around the gather would measure the slower of them
            # twice and say nothing about the other. Their sum can therefore
            # exceed the elapsed time they ran inside, which is what
            # overlapping means and is not a bug in the numbers.
            (vague_result, vague_ms), (score_result, score_ms) = await gather_or_cancel(
                _timed(
                    detect_vagueness(
                        deps.llm,
                        note,
                        note_type,
                        scope=scope,
                        config=config,
                        settings=deps.settings,
                    )
                ),
                _timed(
                    score_note(
                        deps.llm,
                        note,
                        note_type,
                        scope=scope,
                        config=config,
                        settings=deps.settings,
                    )
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
            # decide() is PURE, so it is asked twice: once with the window
            # assumed open, whose answer says which db2 trip (if any) the two
            # guards owe, and once with what the store actually said.
            attempts, rate_allowed, rate_count = await _rate_limit_trip(
                decide(
                    score,
                    analysis,
                    attempts=attempts,
                    rate_allowed=True,
                    rate_count=0,
                    config=config,
                    resubmission=resubmission,
                ),
                scope,
                request,
                attempts=attempts,
                config=config,
            )
            decision = decide(
                score,
                analysis,
                attempts=attempts,
                rate_allowed=rate_allowed,
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

        # --- step 9: confirm. The judgement exists -- scored, or suppressed by
        # the classifier -- so the reservation takes the long TTL. INSIDE the
        # try, so a cancellation that lands during it still releases; BEFORE
        # the outcome line, so nothing is logged as judged whose key is not
        # settled. Fails OPEN inside state.py, so it cannot raise into the
        # release path.
        await state.confirm_idempotency(
            scope.tenant,
            request.note_id,
            fingerprint,
            ttl=config.idempotency_ttl_seconds,
            request_id=scope.request_id,
        )
    except BaseException:
        # Reserved, then did not finish: release so the caller can retry
        # instead of being told 409 for a judgement that never happened.
        # BaseException, because CancelledError is not an Exception -- and the
        # deadline, a client disconnect and a worker shutdown all arrive as
        # one. The shield keeps the DELETE running though the awaiting task is
        # already cancelled: a second cancellation, which a shutdown or a
        # cancel scope delivers, would otherwise kill it on the wire.
        await asyncio.shield(
            state.release_idempotency(
                scope.tenant, request.note_id, fingerprint, request_id=scope.request_id
            )
        )
        raise

    # --- step 10: the resubmission reference, only for a prompt actually sent
    #
    # OUTSIDE the try, deliberately. The judgement exists: the note was
    # fetched, three calls were paid for, a decision was made. A db2 outage now
    # must not release the reservation and turn an answered request into a 503,
    # so this call is placed where a raise could not reach the release path --
    # as well as failing open inside state.py. Both guards, because one of
    # them is a promise another module keeps.
    #
    # decision is None on every suppressed judgement, so a suppressed note
    # never writes one -- and neither does a resubmission, which withholds its
    # prompt by policy and so never reaches prompt_sent. Neither counter is
    # here: both were taken at step 8, by the check itself.
    if decision is not None and decision.prompt_sent:
        # Register item 33: the note we are prompting on, recorded beside the
        # counter it belongs to, at the one moment prompt_sent becomes true.
        await state.write_attempt_fingerprint(
            scope.tenant,
            request.note_id,
            fingerprint,
            ttl=config.attempt_ttl_seconds,
            request_id=scope.request_id,
        )

    _log_outcome(
        scope,
        judgement,
        model_passes=model_passes,
        # Read LAST, after the reference: elapsed_ms is what the caller waited
        # for, and the caller was still waiting through step 10.
        timings=_Timings(
            elapsed_ms=_ms_since(started),
            classify_ms=classify_ms,
            vague_ms=vague_ms,
            score_ms=score_ms,
        ),
        author_differs_from_subject=author_differs_from_subject,
    )
    return judgement


async def _rate_limit_trip(
    provisional: Decision,
    scope: TenantScope,
    request: _JudgementInput,
    *,
    attempts: int,
    config: TenantConfig,
) -> tuple[int, bool, int]:
    """The ONE db2 trip this judgement's two prompt guards take, or none at all.

    `provisional` is decide() with the window assumed open and `attempts` as read
    at step 5, so the documented order (resubmission, attempt cap, rate limit,
    nothing to ask) picks the trip and no condition is restated here:

      a prompt would be sent   TAKE the note's attempt and the subject's slot
                               together -- this IS both increments, and a
                               request that lost the note's attempt gets the cap.
      nothing to ask           READ the rate limit only, so an exhausted window
                               still reports `rate_limited` ahead of
                               `nothing_to_ask`.
      anything else            NO call (resubmission, attempt cap, accept_silent).

    Returns (attempts, rate_allowed, rate_count) for the second decide().
    """
    # ASSUMPTION[Q7]: keyed on scope.subject -- who is ASKING -- not on the
    # note's author_id; the person we would pester is the one making the request.
    if provisional.prompt_sent:
        return await state.take_prompt_slots(
            scope.tenant,
            request.note_id,
            scope.subject,
            attempt_cap=config.clarification_cap,
            attempt_ttl=config.attempt_ttl_seconds,
            rate_limit=config.rate_limit_per_hour,
            rate_ttl=config.rate_limit_window_seconds,
            attempts_read=attempts,
            request_id=scope.request_id,
        )
    if provisional.prompt_withheld is PromptWithheld.NOTHING_TO_ASK:
        return (
            attempts,
            True,
            await state.read_rate_limit(
                scope.tenant, scope.subject, request_id=scope.request_id
            ),
        )
    return attempts, True, 0


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
    timings: _Timings,
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

    THE FIVE NUMBERS (register item 72) are on BOTH outcome events and on both
    routes, because the question they answer -- "why is this slow, and was the
    pod busy at the time?" -- is asked of the judgements that were suppressed
    exactly as often as of the ones that were scored. They are numbers and
    nothing else: four durations on the monotonic clock and a count of requests
    in flight, none of them derived from a note and none of them capable of
    being. See `_Timings` for why null is the right value for a pass that did
    not run, and for why the three do not add up to `elapsed_ms`.

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
                **timings.fields(),
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
            **timings.fields(),
            **author_field,
        },
    )
