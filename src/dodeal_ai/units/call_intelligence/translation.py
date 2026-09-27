"""Translating a done call (Unit B): the stage-1 transcript into Arabic or
English, asked for by the CRM and sent back as call.translation.

  POST /api/v1/calls/jobs/{job_id}/translation {target}  202, 200 held; 409
      result_expired once the stage-1 result is gone, 409 already_in_language
      for a call mostly in the target language already; 429 at the calls
      budget, 503 when the store that counts it cannot say (M9)

A REPEAT REQUEST ANSWERS FROM THE HELD TRANSLATION (status done, the
translation beside it) and queues nothing; a held failure is queued again.
"queued" is answered only when a run will come: the task keeps no arq result
(workers/calls.py), so a run that held nothing never blocks the next one.

THE PASS, unit_b.translate, runs on the stage-2 queue (translate_call): the
transcript as every pass reads it -- the prompt copy, numbers masked -- in
chunks of at most CHUNK_CHARS characters, each answer under a 2500-token
ceiling. Every segment keeps its id, its speaker and its times; only its text
is the model's. THE CHECKS, in code: each chunk's ids answered once each and
no other, the chunk's text in the target's script (evidence.in_language).

PAID ONCE: a chunk the model never answered is asked once more; a malformed
answer gets its one reprompt and then fails the translation. A reprompt that
got no answer is sent once more alone, the first answer read back unpaid
(paid.ask_twice). The task is never re-run (max_tries 1), so no answered
chunk is paid for twice.

THE CALLS BUDGET is checked before any chunk is paid for, in the request and
again in the task: over it, or with its store down, the translation fails
unpaid with translate_<reason>.

THE RESULT is held beside the stage-1 result for the tenant's result hold,
readable through the status route, and sent signed to the callback as
call.translation: one attempt inline, then the normal callback schedule
(delivery.py), on a delivery state of its own per target. The model is never
asked again for a send that failed: every retry reads the held result. A HOLD
THAT FAILS still sends call.translation: the failure translate_store_unavailable
is held in its place when the store takes it, and the event goes either way.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext, TenantScope
from dodeal_ai.core.cost.limiter import CallsBudgetPaused, calls_budget_preflight
from dodeal_ai.core.errors import (
    AlreadyInLanguage,
    CallJobNotFound,
    CallsBudgetUnavailable,
    JobStoreUnavailableResponse,
    MalformedOutputError,
    ModelUnavailableError,
    ResultExpired,
    TokenBudgetExceeded,
)
from dodeal_ai.core.jobs import (
    Job,
    JobStoreUnavailable,
    owe_translation_delivery,
    read_job,
    read_result,
    read_translation,
    store_translation,
)
from dodeal_ai.core.llm import LLMClient
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_TRANSLATE, task_ceiling
from dodeal_ai.units.call_intelligence.config import resolve_calls_config
from dodeal_ai.units.call_intelligence.delivery import translation_event
from dodeal_ai.units.call_intelligence.evidence import CallText, Errors, in_language
from dodeal_ai.units.call_intelligence.paid import ask_twice
from dodeal_ai.units.call_intelligence.prompts import (
    PROMPT_SET_VERSION,
    REPROMPT_TAIL_TEMPLATE,
    REPROMPT_TAILS,
    TRANSLATE_TEMPLATE,
    build_call_prompt,
    clock,
    one_line,
    said_by,
    segment_id,
)
from dodeal_ai.units.call_intelligence.queues import enqueue_translation
from dodeal_ai.units.call_intelligence.transcriber import LanguageProfile, Transcript
from dodeal_ai.units.call_intelligence.worker import (
    Deliver,
    deliver_nothing,
    job_scope,
    tenant_client,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

_logger = logging.getLogger("dodeal_ai.unit_b")

TRANSLATE_LABEL = "llm.unit_b.translate"
# The reason a translation that could not be held is sent with.
STORE_DOWN = "translate_store_unavailable"

# Each chunk's answer ceiling (the brief): a chunk's translation and its ids.
TRANSLATE_MAX_OUTPUT_TOKENS = 2500

# The most source characters one chunk carries: Arabic runs to three tokens a
# word, so 3000 characters translate well inside the 2500-token ceiling.
CHUNK_CHARS = 3000

# The call already in the target language: nothing to translate.
_ALREADY = {
    "ar": LanguageProfile.MOSTLY_AR,
    "en": LanguageProfile.MOSTLY_EN,
}

type Target = Literal["ar", "en"]


class TranslationRequest(BaseModel):
    """What the CRM asks for: the language to translate the call into."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: Target


class TranslationAccepted(BaseModel):
    """queued: a run will come. done: the held translation, answered again."""

    job_id: str
    target: Target
    status: Literal["queued", "done"] = "queued"
    translation: dict[str, object] | None = None


class _Line(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    segment: Annotated[str, Field(pattern=r"^s[1-9][0-9]{0,4}$")]
    text: Annotated[str, Field(max_length=4000)]


class Translated(BaseModel):
    """unit_b.translate's answer for one chunk, exactly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segments: list[_Line]


async def request_translation(
    context: RequestContext, job_id: str, body: TranslationRequest
) -> TranslationAccepted:
    """The held translation, done; else queued within the calls budget; 404,
    409 when there is nothing to do, 429 or 503 for the budget."""
    tenant, target = context.tenant, body.target
    try:
        job = await read_job(tenant, job_id)
        result = None if job is None else await read_result(tenant, job_id)
        held = None if job is None else await read_translation(tenant, job_id, target)
    except JobStoreUnavailable:
        raise JobStoreUnavailableResponse() from None
    if job is None:
        raise CallJobNotFound()
    if result is None or result.get("transcript") is None:
        raise ResultExpired()
    transcript = Transcript.model_validate(result["transcript"])
    if transcript.language_profile is _ALREADY[target]:
        raise AlreadyInLanguage()
    if held is not None and held.get("segments") is not None:
        return TranslationAccepted(
            job_id=job_id, target=target, status="done", translation=held
        )
    try:
        await calls_budget_preflight(job_scope(job))
    except CallsBudgetPaused as paused:
        if paused.reason_code == "cost_store_unavailable":
            raise CallsBudgetUnavailable() from None
        raise TokenBudgetExceeded() from None
    await enqueue_translation(tenant, job_id, target)
    return TranslationAccepted(job_id=job_id, target=target)


def chunks(call: CallText) -> list[list[int]]:
    """Segment indices in runs of at most CHUNK_CHARS shown characters; a
    segment longer than that is a chunk of its own."""
    runs: list[list[int]] = []
    size = CHUNK_CHARS
    for index, text in enumerate(call.shown):
        if size + len(text) > CHUNK_CHARS:
            runs.append([])
            size = 0
        runs[-1].append(index)
        size += len(text)
    return runs


def translate_data(call: CallText, run: Sequence[int], target: str) -> str:
    """The chunk's segments with their call-wide ids, then the target."""
    lines = "\n".join(
        f"[{segment_id(n)} {clock(call.segments[n].start_s)} "
        f"{said_by(call.segments[n])}] {one_line(call.shown[n])}"
        for n in run
    )
    return f"TRANSCRIPT:\n{lines}\n\nTARGET: {target}"


def check_translation(
    run: Sequence[int], target: Target
) -> Callable[[Translated], None]:
    """Every id of the chunk answered once and no other; the text in the
    target's script."""
    wanted = [segment_id(n) for n in run]

    def check(answer: Translated) -> None:
        errors: Errors = []
        if [line.segment for line in answer.segments] != wanted:
            errors.append(("segments", "ids_not_kept"))
        if not in_language(" ".join(line.text for line in answer.segments), target):
            errors.append(("segments", "wrong_language"))
        if errors:
            raise output_rejected(TRANSLATE_LABEL, tuple(errors))

    return check


async def _chunk(
    client: LLMClient,
    call: CallText,
    run: Sequence[int],
    target: Target,
    *,
    scope: TenantScope,
    settings: Settings,
) -> Translated:
    """One chunk, asked once more only when no answer arrived."""

    async def ask(replay: LLMClient) -> Translated:
        answer, _ = await call_model(
            replay,
            build_call_prompt(TRANSLATE_TEMPLATE, translate_data(call, run, target)),
            Translated,
            TRANSLATE_LABEL,
            scope=scope,
            settings=settings,
            profile=PROFILE_UNIT_B_TRANSLATE,
            max_output_tokens=task_ceiling(
                settings,
                PROFILE_UNIT_B_TRANSLATE,
                plain=TRANSLATE_MAX_OUTPUT_TOKENS,
                reasoning=TRANSLATE_MAX_OUTPUT_TOKENS,
                client=client,
            ),
            check=check_translation(run, target),
            reprompt_tail=REPROMPT_TAIL_TEMPLATE,
            tail_by_error=REPROMPT_TAILS,
        )
        return answer

    return await ask_twice(client, ask)


async def translate_call(
    ctx: dict[str, Any], tenant: str, job_id: str, target: Target
) -> None:
    """The stage-2 queue's translation task: translate, hold, and send on the
    callback schedule."""
    job = await read_job(tenant, job_id)
    result = None if job is None else await read_result(tenant, job_id)
    if job is None or result is None or result.get("transcript") is None:
        return
    config = await resolve_calls_config(tenant)
    client = tenant_client(ctx, config)
    transcript = Transcript.model_validate(result["transcript"])
    call = CallText.of(transcript, country_code=config.phone_country_code)
    translation: dict[str, object] = {
        "target": target,
        "segments": None,
        "reason": None,
        "versions": {"prompt": PROMPT_SET_VERSION},
    }
    try:
        if client is None:
            raise ModelUnavailableError()
        await calls_budget_preflight(job_scope(job))
        lines: list[dict[str, object]] = []
        for run in chunks(call):
            answer = await _chunk(
                client, call, run, target, scope=job_scope(job), settings=get_settings()
            )
            lines += [
                {
                    "segment": line.segment,
                    "start_s": call.segments[n].start_s,
                    "end_s": call.segments[n].end_s,
                    "speaker": said_by(call.segments[n]),
                    "text": line.text,
                }
                for n, line in zip(run, answer.segments, strict=True)
            ]
        translation["segments"] = lines
    except CallsBudgetPaused as paused:
        translation["reason"] = f"translate_{paused.reason_code}"
    except ModelUnavailableError:
        translation["reason"] = "translate_model_unavailable"
    except MalformedOutputError:
        translation["reason"] = "translate_malformed_output"
    await _hold(job, target, translation, ttl_seconds=config.result_ttl_seconds)
    _logger.info(
        "call_translation_done",
        extra={
            "tenant": tenant,
            "job_id": job_id,
            "target": target,
            "reason": translation["reason"],
        },
    )
    if config.callback_url is None:
        return
    try:
        if not await owe_translation_delivery(job, target, now=datetime.now(UTC)):
            return
    except JobStoreUnavailable:
        _logger.warning(
            "call_translation_not_sent",
            extra={"tenant": tenant, "job_id": job_id, "reason_code": STORE_DOWN},
        )
        return
    # One attempt now; a failure is left to the schedule (delivery.py).
    deliver: Deliver = ctx.get("deliver", deliver_nothing)
    await deliver(job, translation_event(target), config)


async def _hold(
    job: Job, target: str, translation: dict[str, object], *, ttl_seconds: int
) -> None:
    """Hold the translation; a store that refuses it holds the failure
    translate_store_unavailable instead when it can, the answer dropped: the
    event goes either way, and nothing is paid for again."""
    try:
        await store_translation(
            job.tenant, job.job_id, target, translation, ttl_seconds=ttl_seconds
        )
        return
    except JobStoreUnavailable:
        _logger.warning(
            "call_translation_hold_failed",
            extra={
                "tenant": job.tenant,
                "job_id": job.job_id,
                "reason_code": STORE_DOWN,
            },
        )
    translation["segments"], translation["reason"] = None, STORE_DOWN
    try:
        await store_translation(
            job.tenant, job.job_id, target, translation, ttl_seconds=ttl_seconds
        )
    except JobStoreUnavailable:
        # The event still goes; its body reads no translation held.
        pass
