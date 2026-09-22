"""judge_note — the Unit A pipeline, in the one order it may run in.

TWO ENTRY POINTS, ONE PIPELINE. `judge_note` FETCHES the note by id (Design A,
the contract route). `judge_note_direct` is handed the note in the request body
by the CRM, which has just saved it (DECISION[DIRECT_ROUTE]). They differ in
steps 1 and 2 and in nothing else: both join `_judge` at step 3 and every step
from there down is one shared function, so the two routes cannot drift into
judging the same text differently.

The order is not incidental; each step is placed where it is because of what
the step after it costs:

  1. fetch the lead                      404 lead_not_found
  2. fetch page one of its notes, match  404 note_not_found
     (1 and 2 start together, register item 9; the lead's error always
      wins, register item 89; a note not found is re-read once, item 17)
     (the direct route skips 1 and 2: the CRM sent the note, and no
      LeadsClient call is made at all)
  3. too thin, or too long?              -> suppressed, STOP. No reservation,
                                            no model call, nothing spent. A
                                            too-thin note still takes its
                                            prompt slots for a FIXED question
                                            (item 64) -- capped, never charged.
  4. reserve idempotency, SHORT          409 duplicate / 503 unavailable;
                                         a confirmed duplicate replays 200
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
import re
import time
from collections.abc import Awaitable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from pydantic import ValidationError

from dodeal_ai.core import metrics
from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.cost.limiter import token_preflight
from dodeal_ai.core.errors import (
    BackendRejectedError,
    BackendUnavailableError,
    DodealError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    JudgementDeadlineExceeded,
    LeadNotFoundError,
    NoteNotFoundError,
)
from dodeal_ai.core.inflight import current_inflight
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.redaction import Redaction, redact
from dodeal_ai.core.resilience import (
    ExternalCallError,
    gather_first_wins,
    gather_or_cancel,
)
from dodeal_ai.schemas.lead import Lead, LeadNote
from dodeal_ai.tools.errors import (
    BackendEnvelopeInvalid,
    BackendForbidden,
    BackendNotFound,
    BackendRejected,
    BackendUnauthorized,
)
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.tools.leads import GET_LEAD_LABEL, LeadsClient
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import (
    CLASSIFY_LABEL,
    classify,
    suppression_for,
)
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.decide import (
    decide,
    enforcement,
    withheld_reason,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Decision,
    DirectJudgementRequest,
    HistoryJudgementRequest,
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
# v2 of the rubric: register item 131 replaced the five marks with twelve
# checks. v3 of the prompt set: item 133 rewrote it as a shared block plus type
# blocks (v2), and item 141 then made ns_closure require a reason and not only
# an ending. Keeping the older prompt files readable is only worth anything if
# the stamp moves with them -- a judgement stamped v2 must be reproducible from
# the v2 text, which it is not once a v2 file has been edited in place.
RUBRIC_VERSION = "note_rubric_v2"
PROMPT_SET_VERSION = "unit_a_prompts_v3"

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

# The daily rate-limit key's window (register item 66): a calendar day, fixed
# here rather than on TenantConfig. Only the CAP varies per tenant; a shorter
# window here would let a subject clear the daily cap before the day is over.
RATE_LIMIT_DAY_WINDOW_SECONDS = 86400

# Register item 64: a note below the length floor still gets a question, with
# no model call to write one. Matched on any Arabic-script letter in the note's
# OWN text (never the redacted copy, which is what the length gate itself
# reads) rather than a full language model, since this is a two-way switch.
_ARABIC_SCRIPT_PATTERN = re.compile("[؀-ۿ]")
_FIXED_CLARIFICATION_PROMPT_AR = (
    "ماذا حدث، وماذا قال العميل، وما الخطوة التالية مع موعدها؟"
)
_FIXED_CLARIFICATION_PROMPT_EN = (
    "What happened, what did the client say, and what is the next step with a date?"
)


def _fixed_clarification_prompt(text: str) -> str:
    """The length gate's fixed question, in the note's own script."""
    return (
        _FIXED_CLARIFICATION_PROMPT_AR
        if _ARABIC_SCRIPT_PATTERN.search(text)
        else _FIXED_CLARIFICATION_PROMPT_EN
    )


# Register item 17: a note missing from page one is read again once, this long
# after the first read, for a save the CRM has not yet made visible to its
# reads. Only when this much of the judgement deadline is left; else 404 at once.
NOTE_REREAD_DELAY_SECONDS = 0.25
NOTE_REREAD_MIN_REMAINING_SECONDS = 1.0


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


# Any request shape. All carry lead_id and note_id, which is all the shared
# pipeline reads off a request -- the direct body's three extra fields are
# unpacked into a Lead and a LeadNote by judge_note_direct before the shared
# function is entered, so nothing below this line knows which route it is on.
_JudgementInput = JudgementRequest | DirectJudgementRequest | HistoryJudgementRequest


class ReplayedJudgement(Judgement):
    """A judgement answered from the idempotency store, not judged again
    (register items 1 and 2). Same fields; the type is how the route knows to
    send Idempotent-Replay: true."""


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
    clarification_prompt: str | None = None,
    prompt_withheld: PromptWithheld | None = None,
    stage_change: bool = False,
    resubmission: bool = False,
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

    `clarification_prompt`/`prompt_withheld` (register item 64) are null for
    every suppression except a note below the length floor, which carries a
    fixed question instead of a model-written one.

    THE ENFORCEMENT BLOCK IS HERE TOO (register item 142), derived from the
    detail: a suppressed judgement has no Decision, and that is exactly why it
    needs the block -- the CRM has no action to read and must not be left to
    infer one from an absent field.
    """
    return Judgement(
        note_id=request.note_id,
        lead_id=request.lead_id,
        author_id=author_id,
        analysis=analysis or NoteAnalysis(),
        score=None,
        decision=None,
        suppressed=Suppressed(
            reason=reason,
            detail_code=detail,
            clarification_prompt=clarification_prompt,
            prompt_withheld=prompt_withheld,
        ),
        enforcement=enforcement(
            config,
            detail=detail,
            stage_change=stage_change,
            resubmission=resubmission,
        ),
        versions=_versions(config, model_version),
        request_id=scope.request_id,
    )


# Punctuation a note picks up in passing: "na," "no answer." "cb!". Stripped
# from the END of a word only -- a leading character belongs to the word in
# both scripts, and stripping both ends would let a stray bracket make a code.
# Wrong value: too few and "na," stops being a code; too many and a word ending
# in one is silently a different word.
_TRAILING_PUNCTUATION = ".,!?;:…،؛؟"


def _short_note_words(text: str) -> list[str]:
    """The note as casefolded words with trailing punctuation removed."""
    return [
        stripped
        for word in text.casefold().split()
        if (stripped := word.rstrip(_TRAILING_PUNCTUATION))
    ]


def _is_short_note_code(word: str, config: TenantConfig) -> bool:
    """A tenant code, with or without its trailing attempt number: na, cb1."""
    bare = word.rstrip("0123456789")
    return bool(bare) and bare in config.short_note_codes


def _is_recognised_short_note(text: str, config: TenantConfig) -> bool:
    """Register item 132: is this below-floor note a known outcome?

    Yes if a tenant phrase OPENS the note and only fillers follow it, or if
    every word is a tenant code, a bare attempt number or a filler AND AT LEAST
    ONE OF THEM IS A CODE. That last clause is register item 137: without it
    "tmrw", "again", "pm" and "بكرة" are each nothing but fillers, so a note
    recording no outcome at all bought three paid calls.

    The phrase matches a PREFIX rather than the whole note, so "لا يرد بكرة" is
    the same outcome as "لا يرد". Only fillers may follow it: a phrase trailed
    by ordinary words is a real note that happens to start with one.

    The tables are stored casefolded, so nothing is rebuilt per call.
    """
    words = _short_note_words(text)
    if not words:
        return False
    for phrase in config.short_note_phrases:
        opening = phrase.split()
        if words[: len(opening)] == opening and all(
            word in config.short_note_fillers for word in words[len(opening) :]
        ):
            return True
    return any(_is_short_note_code(word, config) for word in words) and all(
        _is_short_note_code(word, config)
        or word.isdigit()
        or word in config.short_note_fillers
        for word in words
    )


def _below_floor(text: str, config: TenantConfig) -> bool:
    """Below the character floor OR the token floor, on the stripped text.

    ONE statement of the floor, because two things ask it: the gate below, and
    `_recognised_short`, which reports whether the gate let a short note
    through. A second copy is how the two start disagreeing about what "the
    text" is -- the fault the gate's own docstring names.
    """
    return (
        len(text) < config.min_note_chars or len(text.split()) < config.min_note_tokens
    )


def _recognised_short(note: LeadNote, config: TenantConfig) -> bool:
    """Register item 132: did the too-short branch RECOGNISE this note?

    True only for a note the floor would have suppressed and the tenant's
    phrases or codes let through. False for a note the floor never touched: a
    full note that happens to open with "not interested" is not a recognised
    short note, and a flag that said otherwise would put the wrong notes in
    front of whoever is deciding where the floor belongs.
    """
    text = note.note.strip()
    return _below_floor(text, config) and _is_recognised_short_note(text, config)


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
    if _below_floor(text, config):
        # Register item 132: a short note that is a known outcome carries on.
        if _is_recognised_short_note(text, config):
            return None
        return (SuppressedReason.INSUFFICIENT_EVIDENCE, SuppressedDetail.NOTE_TOO_SHORT)
    if len(text) > config.max_note_chars:
        return (SuppressedReason.NOT_SCORABLE, SuppressedDetail.NOTE_TOO_LONG)
    return None


async def _fixed_question(
    scope: TenantScope,
    request: _JudgementInput,
    note: LeadNote,
    detail: SuppressedDetail,
    *,
    config: TenantConfig,
    resubmission: bool,
    history: bool,
) -> tuple[str | None, PromptWithheld | None]:
    """Register item 64: the length gate's fixed question, for a too-short note
    only -- a too-long note (NOTE_TOO_LONG) gets neither field.

    No model wrote this question, but it is still capped like one would be: it
    takes the note's attempt slot and the subject's hourly and daily rate slots
    in the SAME trip a model question takes (state.take_prompt_slots), so a
    salesperson cannot be asked more often for writing a thin note than for
    writing a vague one. A resubmission withholds outright, the same reason a
    scored judgement's resubmission does, and touches no counter. So does a
    history judgement (register item 127), with its own reason.
    """
    if detail is not SuppressedDetail.NOTE_TOO_SHORT:
        return None, None
    prompt = _fixed_clarification_prompt(note.note)
    if history:
        return prompt, PromptWithheld.HISTORY
    if resubmission:
        return prompt, PromptWithheld.RESUBMISSION
    attempts = await state.read_attempts(
        scope.tenant, request.note_id, request_id=scope.request_id
    )
    attempts, rate_allowed, rate_count = await state.take_prompt_slots(
        scope.tenant,
        request.note_id,
        scope.subject,
        attempt_cap=config.clarification_cap,
        attempt_ttl=config.attempt_ttl_seconds,
        rate_limit=config.rate_limit_per_hour,
        rate_ttl=config.rate_limit_window_seconds,
        rate_limit_day=config.rate_limit_per_day,
        rate_ttl_day=RATE_LIMIT_DAY_WINDOW_SECONDS,
        attempts_read=attempts,
        request_id=scope.request_id,
    )
    withheld = withheld_reason(
        NoteAnalysis(clarification_prompt=prompt),
        attempts=attempts,
        rate_allowed=rate_allowed,
        rate_count=rate_count,
        config=config,
        resubmission=False,
    )
    return prompt, withheld


async def _fetch_note(
    scope: TenantScope,
    request: JudgementRequest,
    deps: JudgementDeps,
    *,
    deadline: float,
) -> tuple[Lead, LeadNote]:
    """Fetch the lead and page one of its notes together, then find the note.

    Both come back. The lead is not fetched only to prove it exists: four of its
    fields are the classifier's context section, and the alternative -- fetching
    it twice, or passing the id and letting the classifier fetch -- would make
    the number of backend calls depend on the note type.

    ASSUMPTION[Q8]: the target note is on PAGE ONE (notes come back newest
    first, 25 per page), so an un-paged fetch finds it. A miss is read again
    ONCE, after NOTE_REREAD_DELAY_SECONDS and only with a second of deadline
    left (register item 17) -- the same page, never a second one; if a note can
    fall off page one, the fix is query parameters in tools/leads.py.

    Design A: the note is matched BY ID, never taken by position. "The newest
    note" would judge whatever arrived most recently, which on a busy lead is
    not the note the caller asked about.

    TYPED BACKEND ERRORS (register item 89, audit H2). The lead's 404 is 404
    lead_not_found; any other 4xx, the notes' 404 included, is 503
    backend_rejected; 401, 403, a missing key and a failure that outlived its
    retry are 503 backend_unavailable. `deadline` is the loop time the
    judgement ends at, so no retry is scheduled past it.
    """
    # Only judge_note reaches this function, and only the fetch route reaches
    # judge_note -- so a None client here would mean the direct entry point had
    # grown a fetch, which is the one thing this route may not do.
    leads = deps.leads
    assert leads is not None
    with _backend_errors(scope):
        # Register item 9: both reads start together. The lead's error is the
        # one raised whenever the lead fails, even if the notes failed first.
        lead, notes = await gather_first_wins(
            leads.get_lead(scope, request.lead_id, deadline=deadline),
            leads.get_lead_notes(scope, request.lead_id, deadline=deadline),
        )
    note = _matching(notes, request.note_id)
    if note is None:
        loop = asyncio.get_running_loop()
        reread = deadline - loop.time() >= NOTE_REREAD_MIN_REMAINING_SECONDS
        _logger.info(
            "note_not_on_first_read",
            extra={
                "tenant": scope.tenant,
                "request_id": scope.request_id,
                "reread": reread,
            },
        )
        if reread:
            await asyncio.sleep(NOTE_REREAD_DELAY_SECONDS)
            with _backend_errors(scope):
                notes = await leads.get_lead_notes(
                    scope, request.lead_id, deadline=deadline
                )
            note = _matching(notes, request.note_id)
    if note is None:
        raise NoteNotFoundError()
    return lead, note


def _matching(notes: list[LeadNote], note_id: int) -> LeadNote | None:
    """The note with this id, matched by id and never by position (Design A)."""
    return next((note for note in notes if note.id == note_id), None)


@contextmanager
def _backend_errors(scope: TenantScope) -> Iterator[None]:
    """Every backend failure inside, as the enumerated error the CRM is told."""
    try:
        yield
    except BackendNotFound as exc:
        if exc.label == GET_LEAD_LABEL:
            raise _backend_failure(scope, exc, LeadNotFoundError()) from None
        raise _backend_failure(scope, exc, BackendRejectedError()) from None
    except BackendRejected as exc:
        raise _backend_failure(scope, exc, BackendRejectedError()) from None
    except (
        ExternalCallError,
        BackendKeyError,
        BackendUnauthorized,
        BackendForbidden,
        BackendEnvelopeInvalid,
    ) as exc:
        raise _backend_failure(scope, exc, BackendUnavailableError()) from None


# backend_errors_total{kind} (register item 22): one word per failure class.
_BACKEND_ERROR_KINDS: dict[type[Exception], str] = {
    BackendNotFound: "not_found",
    BackendRejected: "rejected",
    BackendUnauthorized: "unauthorized",
    BackendForbidden: "forbidden",
    BackendKeyError: "key_missing",
    ExternalCallError: "unavailable",
    BackendEnvelopeInvalid: "envelope_invalid",
}


def _backend_failure(
    scope: TenantScope, exc: Exception, error: DodealError
) -> DodealError:
    """Log the fetch that failed and hand back the error to raise. The error
    TYPE only: the watchdog and the tool layer already logged the rest."""
    metrics.BACKEND_ERRORS.labels(kind=_BACKEND_ERROR_KINDS[type(exc)]).inc()
    _logger.warning(
        f"judgement_{error.reason_code}",
        extra={
            "reason_code": error.reason_code,
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "error_type": type(exc).__name__,
        },
    )
    return error


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
    return await _metered(
        started,
        _judge_fetched(
            scope, request, resubmission=resubmission, deps=deps, started=started
        ),
    )


async def _judge_fetched(
    scope: TenantScope,
    request: JudgementRequest,
    *,
    resubmission: bool,
    deps: JudgementDeps,
    started: float,
) -> Judgement:
    """judge_note's body: the deadline, the fetch, then the shared steps."""
    # The deadline starts with the clock, so the two backend reads spend it too.
    try:
        async with asyncio.timeout(deps.settings.judgement_deadline_seconds) as budget:
            deadline = budget.when()
            assert deadline is not None  # set: the timeout was given a delay
            lead, note = await _fetch_note(scope, request, deps, deadline=deadline)
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

    CREDENTIAL (register item 92): the CRM calls with its SERVICE token, and
    the route builds `scope` with `scope_for_author(request.author_id)`. So the
    scope's subject is `author:<id>` -- the CRM's stored author, asserted by
    the CRM and marked so -- and the clarification caps and the per-user token
    budget key on that author with no change here. There is no token `sub` on
    this route to compare the author with, so nothing is compared.
    """
    # As on the fetch route, the first statement in the function -- so the two
    # routes' elapsed_ms mean the same thing, minus the fetch this one does not
    # do. Comparing them is how "is the CRM's read surface the slow part?" gets
    # answered when it comes back.
    started = time.monotonic()
    return await _metered(
        started,
        _judge_sent(
            scope,
            request,
            resubmission=resubmission,
            deps=deps,
            started=started,
            # The CRM sends this right after the save, so arrival time stands
            # in for createdAt (item 32). Nothing reads it today.
            created_at=datetime.now(UTC),
            history=False,
        ),
    )


async def judge_note_history(
    scope: TenantScope,
    request: HistoryJudgementRequest,
    *,
    deps: JudgementDeps,
) -> Judgement:
    """Judge an OLD note the CRM sent us, for the measures' past (item 127).

    The direct route's pipeline, with four differences and no more: no
    question is ever sent (every prompt is withheld `history`), no db2 counter
    is read or written and no prompt slot is taken, no resubmission reference
    is written, and the note's own `note_created_at` is its createdAt. The
    route charges the tenant's history token budget through `scope`. The score,
    the action and the enforcement verdict are computed as for a live note.
    """
    started = time.monotonic()
    return await _metered(
        started,
        _judge_sent(
            scope,
            request,
            resubmission=False,
            deps=deps,
            started=started,
            created_at=request.note_created_at,
            history=True,
        ),
    )


def _stage_change(request: _JudgementInput, config: TenantConfig) -> bool:
    """Register item 142: is the CRM moving the lead to one of the tenant's
    blocking stages with this note? The ONE boolean the stage becomes; the
    stage itself goes no further than this function -- not to a log line, not
    to a prompt and not to decide.py."""
    stage = getattr(request, "stage_change_to", None)
    return stage is not None and stage.casefold() in config.blocking_stages


async def _judge_sent(
    scope: TenantScope,
    request: DirectJudgementRequest | HistoryJudgementRequest,
    *,
    resubmission: bool,
    deps: JudgementDeps,
    started: float,
    created_at: datetime,
    history: bool,
) -> Judgement:
    """The body of both note-in-the-body entry points: the deadline, the note
    as sent, the shared steps."""
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
                createdAt=created_at,
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
                history=history,
                stage_change=_stage_change(request, deps.config),
            )
    except TimeoutError:
        raise _deadline_exceeded(scope, started) from None


async def _metered(started: float, judging: Awaitable[Judgement]) -> Judgement:
    """Count the judgement's outcome and observe its duration (register item
    22): completed, suppressed, replayed, a reason code, cancelled or error."""
    try:
        judgement = await judging
    except DodealError as exc:
        _observe(exc.reason_code, started)
        raise
    except BaseException as exc:
        _observe(
            "cancelled" if isinstance(exc, asyncio.CancelledError) else "error",
            started,
        )
        raise
    if isinstance(judgement, ReplayedJudgement):
        _observe("replayed", started)
    else:
        _observe(
            "suppressed" if judgement.suppressed is not None else "completed", started
        )
    return judgement


def _observe(outcome: str, started: float) -> None:
    metrics.JUDGEMENTS.labels(outcome=outcome).inc()
    metrics.JUDGEMENT_SECONDS.observe(time.monotonic() - started)


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
    history: bool = False,
    stage_change: bool = False,
) -> Judgement:
    """Every step from the length gate down, for every entry point.

    ONE body, not two. The two routes differ only in how the Lead and the
    LeadNote were obtained; everything that costs money, everything that
    reserves, everything that decides and everything that is logged is here, so
    the two routes cannot drift into judging the same text differently.

    `history` (register item 127) withholds every prompt, reads and writes no
    db2 counter, and reserves under its own idempotency namespace.
    `stage_change` (register item 142) is the one boolean a CRM stage becomes;
    it reaches the enforcement block and nothing else.

    `started` is the entry point's `time.monotonic()` reading, taken there and
    not here so that the fetch route's two backend calls are inside its
    `elapsed_ms`. It is a DURATION baseline and never a timestamp: nothing
    compares it to a clock, and it is not on any line.
    """
    config = deps.config
    operation = state.JUDGE_HISTORY if history else state.JUDGE_NOTE
    # Register item 59: the key is the ORIGINAL text's fingerprint; only the
    # three prompts read the redacted copy. Counts reach the outcome line.
    fingerprint = state.note_fingerprint(note.note)
    redaction = redact(note.note)
    prompt_note = note.model_copy(update={"note": redaction.text})

    # --- length: before any reservation, before any spend -------------------
    # Register item 132: read once, here, and carried to whichever outcome line
    # this judgement reaches. A note the floor would have suppressed and the
    # tenant's table let through is the one input a floor change moves, and it
    # is invisible in the response -- the judgement looks like any other.
    recognised_short = _recognised_short(note, config)
    gated = _length_gate(note, config)
    if gated is not None:
        gate_reason, gate_detail = gated
        clarification_prompt, prompt_withheld = await _fixed_question(
            scope,
            request,
            note,
            gate_detail,
            config=config,
            resubmission=resubmission,
            history=history,
        )
        judgement = _suppressed(
            scope,
            request,
            author_id=author_id,
            reason=gate_reason,
            detail=gate_detail,
            config=config,
            clarification_prompt=clarification_prompt,
            prompt_withheld=prompt_withheld,
            stage_change=stage_change,
            resubmission=resubmission,
        )
        _log_outcome(
            scope,
            judgement,
            model_passes=0,
            # No pass ran, so all three are null. Not zero: this note did not
            # take no time to classify, it was never classified.
            timings=_Timings(elapsed_ms=_ms_since(started)),
            redaction=redaction,
            request_ids=_no_request_ids(),
            recognised_short=recognised_short,
        )
        return judgement

    # --- reserve: the only thing between a double-submit and paying twice ---
    # SHORT: the in-flight lifetime. The long one is the confirm's, at the end
    # of the try below. Rounded UP, never down: a deadline under a quarter-second
    # would otherwise give EX 0, which Redis refuses, and a refused reservation
    # is a 503.
    inflight_ttl = math.ceil(
        deps.settings.judgement_deadline_seconds * IDEMPOTENCY_INFLIGHT_MULTIPLIER
    )
    replay: ReplayedJudgement | None = None
    try:
        claimed = await state.reserve_idempotency(
            scope.tenant,
            request.note_id,
            fingerprint,
            ttl=inflight_ttl,
            request_id=scope.request_id,
            operation=operation,
        )
        if not claimed:
            claimed, replay = await _existing_judgement(
                scope, request, fingerprint, ttl=inflight_ttl, operation=operation
            )
    except state.IdempotencyUnavailableError:
        raise IdempotencyUnavailableResponse() from None
    if replay is not None:
        _logger.info(
            "judgement_replayed",
            extra={
                "tenant": scope.tenant,
                "request_id": scope.request_id,
                **_Timings(elapsed_ms=_ms_since(started)).fields(),
            },
        )
        return replay
    if not claimed:
        raise DuplicateRequestError()

    decision: Decision | None = None
    # Register item 26: each pass's provider request id, null for a pass that
    # did not run -- the handle for reconciling a call with the provider's bill.
    request_ids = _no_request_ids()
    # None until the pass that fills each one actually runs, so a judgement that
    # stopped early carries null rather than a made-up zero.
    classify_ms: int | None = None
    vague_ms: int | None = None
    score_ms: int | None = None
    try:
        # Read HERE, before anything is spent: it picks step 8's trip, and that
        # trip's script re-checks the cap as it takes, so a concurrent request
        # for this note cannot also send. Fails OPEN (0). A history judgement
        # reads no counter at all (register item 127).
        attempts = (
            0
            if history
            else await state.read_attempts(
                scope.tenant,
                request.note_id,
                request_id=scope.request_id,
            )
        )

        # The token budget, read before the first thing that costs money. Over
        # budget is 429 token_budget_exceeded and releases the reservation
        # below like any other non-200 after reserving. Near it, no pass may
        # reprompt (register item 61).
        reprompt = not await token_preflight(scope)

        # --- classify: the first thing that costs money ---------------------
        (classification, classify_response), classify_ms = await _timed(
            classify(
                deps.llm,
                prompt_note,
                lead,
                scope=scope,
                settings=deps.settings,
                reprompt=reprompt,
            )
        )
        model_passes = 1
        request_ids["classify_provider_request_id"] = (
            classify_response.provider_request_id
        )

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
                stage_change=stage_change,
                resubmission=resubmission,
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
                        prompt_note,
                        note_type,
                        scope=scope,
                        config=config,
                        settings=deps.settings,
                        reprompt=reprompt,
                    )
                ),
                _timed(
                    score_note(
                        deps.llm,
                        prompt_note,
                        note_type,
                        scope=scope,
                        config=config,
                        settings=deps.settings,
                        reprompt=reprompt,
                    )
                ),
            )
            model_passes = 3

            vague_output, vague_response = vague_result
            score_output, score_response = score_result
            request_ids["vague_provider_request_id"] = (
                vague_response.provider_request_id
            )
            request_ids["score_provider_request_id"] = (
                score_response.provider_request_id
            )
            analysis = NoteAnalysis(
                note_type=note_type,
                is_vague=vague_output.is_vague,
                missing_components=vague_output.missing_components,
                clarification_prompt=vague_output.clarification_prompt,
                reasoning=vague_output.reasoning,
            )
            score = compute_score(score_output.checks, note_type, config)
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
                    history=history,
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
                history=history,
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
                # From the ACTION, not the total and not the band: the action
                # is what the two thresholds already decided, and deriving the
                # verdict from the total again would be a second place for the
                # thresholds to be read (register item 142).
                enforcement=enforcement(
                    config,
                    action=decision.action,
                    stage_change=stage_change,
                    resubmission=resubmission,
                ),
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
            judgement_json=judgement.model_dump_json(),
            ttl=config.idempotency_ttl_seconds,
            request_id=scope.request_id,
            operation=operation,
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
                scope.tenant,
                request.note_id,
                fingerprint,
                request_id=scope.request_id,
                operation=operation,
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
        redaction=redaction,
        request_ids=request_ids,
        recognised_short=recognised_short,
    )
    return judgement


def _no_request_ids() -> dict[str, str | None]:
    """The three per-pass request id fields, all null until a pass answers."""
    return {
        "classify_provider_request_id": None,
        "vague_provider_request_id": None,
        "score_provider_request_id": None,
    }


async def _existing_judgement(
    scope: TenantScope,
    request: _JudgementInput,
    fingerprint: str,
    *,
    ttl: int,
    operation: str,
) -> tuple[bool, ReplayedJudgement | None]:
    """What a request that lost the reservation gets: (claimed, replay).

    A confirmed judgement for this note is replayed with THIS request's id. A
    key still reserved, or one for another lead, is (False, None): 409. A stored
    value that no longer validates is logged, taken over and judged again.
    """
    stored = await state.read_confirmed_judgement(
        scope.tenant,
        request.note_id,
        fingerprint,
        request_id=scope.request_id,
        operation=operation,
    )
    if stored is None:
        return False, None
    try:
        replay = ReplayedJudgement.model_validate_json(stored)
    except ValidationError:
        # Never the value or pydantic's message: the value holds model output.
        _logger.warning(
            "idempotency_replay_invalid",
            extra={
                "reason_code": "idempotency_replay_invalid",
                "tenant": scope.tenant,
                "request_id": scope.request_id,
            },
        )
        claimed = await state.take_over_idempotency(
            scope.tenant,
            request.note_id,
            fingerprint,
            expected=stored,
            ttl=ttl,
            request_id=scope.request_id,
            operation=operation,
        )
        return claimed, None
    if (replay.note_id, replay.lead_id) != (request.note_id, request.lead_id):
        return False, None
    return False, replay.model_copy(update={"request_id": scope.request_id})


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
    # ASSUMPTION[Q7]: keyed on scope.subject, never compared with author_id. On
    # the fetch routes that is the verified `sub`; on the direct routes it is
    # `author:<id>` (register item 92), so each author has their own caps.
    if provisional.prompt_sent:
        return await state.take_prompt_slots(
            scope.tenant,
            request.note_id,
            scope.subject,
            attempt_cap=config.clarification_cap,
            attempt_ttl=config.attempt_ttl_seconds,
            rate_limit=config.rate_limit_per_hour,
            rate_ttl=config.rate_limit_window_seconds,
            rate_limit_day=config.rate_limit_per_day,
            rate_ttl_day=RATE_LIMIT_DAY_WINDOW_SECONDS,
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
    redaction: Redaction,
    request_ids: dict[str, str | None],
    recognised_short: bool,
) -> None:
    """One structured line per judgement.

    `redacted_phone`, `redacted_email` and `redacted_id` (register item 59) are
    counts of what the prompts did not see -- numbers, never the values.

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

    `recognised_short` (register item 132) is a bool on BOTH outcome lines: true
    only when the note was below the floor and the tenant's phrases or codes
    recognised it, so it was judged rather than suppressed. It answers the only
    question a floor change is decided on -- how often is the table carrying a
    note the floor would have refused -- and nothing in the response says it: a
    recognised note comes back looking like any other. False, never null, on
    every note the floor never touched, because "was not recognised" and "was
    never short" are the same answer to "did the table do anything here".
    """
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
                "recognised_short": recognised_short,
                **timings.fields(),
                **redaction.fields(),
                **request_ids,
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
            "recognised_short": recognised_short,
            **timings.fields(),
            **redaction.fields(),
            **request_ids,
        },
    )
