"""judge_note — the Unit A pipeline, in the one order it may run in.

The order is not incidental; each step is placed where it is because of what
the step after it costs:

  1. fetch the lead                      404 lead_not_found     (see H2 below)
  2. fetch page one of its notes, match  404 note_not_found
  3. thin evidence?                      -> suppressed, STOP. No reservation,
                                            no model call, nothing spent.
  4. reserve idempotency                 409 duplicate / 503 unavailable
  5. read the rate limit                 (fail open)
  6. read the attempt count              (fail open)
  7. SEAM[STEP3] token pre-flight        (no-op today)
  8. classify -> vague -> score          three model calls
  9. compute, decide                     in code, never from the model
 10. increment counters ONLY if a prompt was actually sent

Steps 8-10 are NOT implemented in this phase: the pipeline runs 1-7 for real
and then returns Suppressed(not_scorable, not_implemented). Everything before
the seam -- the fetches, the 404s, the thin-evidence rule, the reservation and
its release, the two fail-open reads -- is real and tested now, so the phase
that fills in the model calls changes only what happens after step 7.

WHY THIS ORDER, at the two places it matters:

  Thin evidence BEFORE the reservation. A three-word note is refused without
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

import logging
from dataclasses import dataclass

from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.cost.limiter import token_preflight
from dodeal_ai.core.errors import (
    BackendUnavailableError,
    DuplicateRequestError,
    IdempotencyUnavailableResponse,
    NoteNotFoundError,
)
from dodeal_ai.core.llm import LLMClient
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.schemas.lead import LeadNote
from dodeal_ai.tools.keys import BackendKeyError
from dodeal_ai.tools.leads import LeadsClient
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.schemas import (
    Judgement,
    JudgementRequest,
    NoteAnalysis,
    Suppressed,
    SuppressedDetail,
    SuppressedReason,
    Versions,
)

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
    """

    leads: LeadsClient
    llm: LLMClient
    config: TenantConfig


def _versions(config: TenantConfig, model_version: str = NO_MODEL) -> Versions:
    return Versions(
        rubric_version=RUBRIC_VERSION,
        prompt_version=PROMPT_SET_VERSION,
        model_version=model_version,
        config_version=config.config_version,
    )


def _suppressed(
    scope: TenantScope,
    request: JudgementRequest,
    *,
    author_id: int,
    reason: SuppressedReason,
    detail: SuppressedDetail,
    config: TenantConfig,
    analysis: NoteAnalysis | None = None,
) -> Judgement:
    """Build a suppressed judgement: score and decision are null, never zero.

    A suppressed note has no score at all. It is not a 0, not a `poor` band and
    not an absent field that something downstream will read as 0 -- so it
    cannot be averaged into a salesperson's figures.
    """
    return Judgement(
        note_id=request.note_id,
        lead_id=request.lead_id,
        author_id=author_id,
        analysis=analysis or NoteAnalysis(),
        score=None,
        decision=None,
        suppressed=Suppressed(reason=reason, detail_code=detail),
        versions=_versions(config),
        request_id=scope.request_id,
    )


def _is_thin(note: LeadNote, config: TenantConfig) -> bool:
    """Too little to judge: below the character floor or the token floor.

    Both, not either: "ok" fails on length, and a long string of one repeated
    word fails on tokens. Whitespace-split is deliberately crude -- it is a
    floor for "is there anything here at all", not a linguistic measure, and it
    behaves the same for Arabic, English and mixed text (one prompt set, no
    language branch).
    """
    text = note.note.strip()
    return (
        len(text) < config.min_note_chars or len(text.split()) < config.min_note_tokens
    )


async def _fetch_note(
    scope: TenantScope, request: JudgementRequest, deps: JudgementDeps
) -> tuple[int, LeadNote]:
    """Fetch the lead, then find the note on page one of its notes.

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
    try:
        await deps.leads.get_lead(scope, request.lead_id)
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
            return note.author_id, note

    raise NoteNotFoundError()


async def judge_note(
    scope: TenantScope,
    request: JudgementRequest,
    *,
    resubmission: bool,
    deps: JudgementDeps,
) -> Judgement:
    """Judge one already-saved note. See the module docstring for the order.

    ASSUMPTION[Q1]: the route this runs behind depends on gate4_cost, i.e. the
    CRM forwards the END USER's JWT and we run Gate 1 (auth) -> Gate 2
    (tenancy) -> Gate 4 (cost) exactly as the probe route does. If the CRM
    turns out to call us as a service instead, the principal source swaps
    behind D1's seam -- this function already takes a TenantScope, not a
    RequestContext, so it does not change; only what builds the scope does, and
    subjects from a service principal get labelled asserted.

    `resubmission` changes what happens to the clarification prompt, never what
    is fetched or scored: attempts are READ and never incremented, and any
    prompt is withheld with prompt_withheld="resubmission". That branch lands
    with the decision step; today both entry points reach the same seam.
    """
    config = deps.config
    author_id, note = await _fetch_note(scope, request, deps)

    # --- thin evidence: before any reservation, before any spend ------------
    if _is_thin(note, config):
        judgement = _suppressed(
            scope,
            request,
            author_id=author_id,
            reason=SuppressedReason.INSUFFICIENT_EVIDENCE,
            detail=SuppressedDetail.NOTE_TOO_SHORT,
            config=config,
        )
        _log_outcome(scope, judgement, model_calls=0)
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

    try:
        # ASSUMPTION[Q7]: the rate limit is keyed on scope.subject -- who is
        # ASKING -- not on note.author_id, who WROTE the note. The limit exists
        # to stop us pestering one person, and the person we would pester is
        # the one making the request. Both reads fail OPEN (0), so a db2 blip
        # cannot 503 a judgement that is otherwise fine.
        await state.read_rate_limit(
            scope.tenant, scope.subject, request_id=scope.request_id
        )
        await state.read_attempts(
            scope.tenant,
            request.lead_id,
            request.note_id,
            request_id=scope.request_id,
        )

        # SEAM[STEP3]: the token budget pre-flight. A no-op that logs once per
        # process; step 3 replaces the body, not this call site.
        await token_preflight(scope)

        # SEAM: classify -> vague -> score -> compute -> decide lands next.
        # Until then this is an honest "we did not judge it", not a zero.
        judgement = _suppressed(
            scope,
            request,
            author_id=author_id,
            reason=SuppressedReason.NOT_SCORABLE,
            detail=SuppressedDetail.NOT_IMPLEMENTED,
            config=config,
        )
    except Exception:
        # Reserved, then failed: release so the caller can retry instead of
        # being told 409 for a judgement that never happened.
        await state.release_idempotency(
            scope.tenant, request.note_id, fingerprint, request_id=scope.request_id
        )
        raise

    _log_outcome(scope, judgement, model_calls=0)
    return judgement


def _log_outcome(scope: TenantScope, judgement: Judgement, *, model_calls: int) -> None:
    """One structured line per judgement.

    Fields only, via extra=, and every one of them is an identifier or a member
    of a fixed vocabulary. NEVER the note text, never the reasoning, never the
    clarification prompt -- the whole point of the unit is that it reads note
    bodies, so this is the log line most likely to leak one.
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
                "model_calls": model_calls,
            },
        )
        return

    assert judgement.score is not None and judgement.decision is not None
    _logger.info(
        "judgement_completed",
        extra={
            "tenant": scope.tenant,
            "request_id": scope.request_id,
            "note_type": judgement.analysis.note_type,
            "band": judgement.score.band.value,
            "action": judgement.decision.action.value,
            "prompt_sent": judgement.decision.prompt_sent,
            "model_calls": model_calls,
        },
    )
