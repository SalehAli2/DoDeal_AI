"""A WhatsApp suggestion written again in a language the CRM names (Unit B).

  POST /api/v1/calls/jobs/{job_id}/whatsapp {language}  200 {job_id,
      language, dialect, text}; 404 for no job of this tenant's; 409
      result_expired once the stage-1 result is gone; 409 whatsapp_in_progress
      while another request writes this language's; counted on the READS
      counter and readable through the status route (reads.py)

THE PASS, unit_b.whatsapp, runs inside the request on the extras profile: the
stored stage-1 transcript as every pass reads it -- the prompt copy, numbers
masked -- then the WHATSAPP LANGUAGE line. One of the product's twelve
languages (language.ProductLanguage); an Arabic code names the dialect too.
THE CHECKS, in code: at most WHATSAPP_MAX_WORDS words, in the language's
script (evidence.written_in); a failure is reprompted once, then 503
malformed_output. A SUGGESTION ONLY, as in extras.py: nothing is sent.

PAID ONCE PER LANGUAGE. A held suggestion is answered from the store, never
asked again; a claim (SET NX) lets one request pay while a second gets 409;
a model that never answered is asked once more (the lead's one exception).
Charged to the calls budget of the call's author, checked first: 429 at the
budget, 503 when the store that counts it cannot say.

HELD NO LONGER THAN THE CALL: the suggestion expires with the stage-1 result
it was written from. Logged by ids, the language and fixed codes only.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from dodeal_ai.core.config import Settings
from dodeal_ai.core.context import RequestContext, TenantScope
from dodeal_ai.core.cost.limiter import CallsBudgetPaused, calls_budget_preflight
from dodeal_ai.core.errors import (
    CallJobNotFound,
    CallsBudgetUnavailable,
    JobStoreUnavailableResponse,
    ModelUnavailableError,
    ResultExpired,
    TokenBudgetExceeded,
    WhatsAppInProgress,
)
from dodeal_ai.core.jobs import (
    JobStoreUnavailable,
    claim_whatsapp,
    read_job,
    read_result,
    read_whatsapps,
    release_whatsapp,
    result_seconds_left,
    store_whatsapp,
)
from dodeal_ai.core.llm import LLMClient, routed
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_EXTRAS, task_ceiling
from dodeal_ai.units.call_intelligence.config import resolve_calls_config
from dodeal_ai.units.call_intelligence.evidence import CallText, Errors, written_in
from dodeal_ai.units.call_intelligence.extras import WHATSAPP_MAX_WORDS
from dodeal_ai.units.call_intelligence.language import (
    ARABIC_LANGUAGES,
    ProductLanguage,
    message_language,
    spoken_of,
)
from dodeal_ai.units.call_intelligence.prompts import (
    PROMPT_SET_VERSION,
    REPROMPT_TAIL_TEMPLATE,
    WHATSAPP_TEMPLATE,
    build_call_prompt,
)
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from dodeal_ai.units.structured_intelligence.llm_call import (
    call_model,
    output_rejected,
)

_logger = logging.getLogger("dodeal_ai.unit_b")

WHATSAPP_LABEL = "llm.unit_b.whatsapp"

# What the answer may cost: a 60-word message in JSON, Arabic at three tokens
# a word; the larger when the profile reasons (register item 116).
WHATSAPP_MAX_OUTPUT_TOKENS = 600
WHATSAPP_REASONING_MAX_OUTPUT_TOKENS = 2000

# How long one request may hold a language's claim: past any honest pass and
# its reprompt, so a crashed request frees it without anyone's help.
WHATSAPP_CLAIM_SECONDS = 180

# The longest message text, in characters, past any 60-word message.
_TEXT_CHARS = 600


class WhatsAppRequest(BaseModel):
    """What the CRM asks for: the language to write the suggestion in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    language: ProductLanguage


class WhatsAppSuggestion(BaseModel):
    """The suggestion, the language and the dialect it was asked in."""

    job_id: str
    language: str
    dialect: str | None
    text: str


class Written(BaseModel):
    """unit_b.whatsapp's answer, exactly."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    whatsapp: Annotated[str, Field(min_length=1, max_length=_TEXT_CHARS)]


def written_language(language: str) -> str:
    """The script family the text is checked in: ar for an Arabic dialect."""
    return message_language(language, "en")


def whatsapp_data(call: CallText, language: str) -> str:
    """The call's data, then the language to write in."""
    return f"{call.data()}\n\nWHATSAPP LANGUAGE: {language}"


def check_written(language: str) -> Callable[[Written], None]:
    """At most WHATSAPP_MAX_WORDS words, in the language's script."""

    def check(answer: Written) -> None:
        errors: Errors = []
        if len(answer.whatsapp.split()) > WHATSAPP_MAX_WORDS:
            errors.append(("whatsapp", "too_long"))
        if not written_in(answer.whatsapp, written_language(language)):
            errors.append(("whatsapp", "wrong_language"))
        if errors:
            raise output_rejected(WHATSAPP_LABEL, tuple(errors))

    return check


async def write_whatsapp(
    client: LLMClient,
    call: CallText,
    language: str,
    *,
    scope: TenantScope,
    settings: Settings,
) -> Written:
    """unit_b.whatsapp: one call, a second when the first answer is malformed,
    and one more only when no answer arrived at all."""

    async def ask() -> Written:
        answer, _ = await call_model(
            client,
            build_call_prompt(WHATSAPP_TEMPLATE, whatsapp_data(call, language)),
            Written,
            WHATSAPP_LABEL,
            scope=scope,
            settings=settings,
            profile=PROFILE_UNIT_B_EXTRAS,
            max_output_tokens=task_ceiling(
                settings,
                PROFILE_UNIT_B_EXTRAS,
                plain=WHATSAPP_MAX_OUTPUT_TOKENS,
                reasoning=WHATSAPP_REASONING_MAX_OUTPUT_TOKENS,
                client=client,
            ),
            check=check_written(language),
            reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        )
        return answer

    try:
        return await ask()
    except ModelUnavailableError:
        return await ask()


def _suggestion(job_id: str, held: dict[str, object]) -> WhatsAppSuggestion:
    return WhatsAppSuggestion(
        job_id=job_id,
        language=str(held["language"]),
        dialect=None if held.get("dialect") is None else str(held["dialect"]),
        text=str(held["text"]),
    )


async def _preflight(scope: TenantScope) -> None:
    """The calls budget, before anything is paid for."""
    try:
        await calls_budget_preflight(scope)
    except CallsBudgetPaused as paused:
        if paused.reason_code == "cost_store_unavailable":
            raise CallsBudgetUnavailable() from None
        raise TokenBudgetExceeded() from None


async def regenerate_whatsapp(
    context: RequestContext,
    job_id: str,
    body: WhatsAppRequest,
    llm: LLMClient,
    settings: Settings,
) -> WhatsAppSuggestion:
    """The suggestion in `body.language`: held, else written, held and
    answered; 404, 409 or the pass's own 503 otherwise."""
    tenant, language = context.tenant, body.language
    try:
        job = await read_job(tenant, job_id)
        result = None if job is None else await read_result(tenant, job_id)
        held = {} if job is None else await read_whatsapps(tenant, job_id, [language])
    except JobStoreUnavailable:
        raise JobStoreUnavailableResponse() from None
    if job is None:
        raise CallJobNotFound()
    if result is None or not isinstance(result.get("transcript"), dict):
        raise ResultExpired()
    kept = held.get(language)
    if isinstance(kept, dict):
        return _suggestion(job_id, kept)
    scope = context.scope_for_author(
        int(str(job.metadata["author_id"])), budget="calls"
    )
    await _preflight(scope)
    try:
        claimed = await claim_whatsapp(
            tenant, job_id, language, ttl_seconds=WHATSAPP_CLAIM_SECONDS
        )
    except JobStoreUnavailable:
        raise JobStoreUnavailableResponse() from None
    if not claimed:
        raise WhatsAppInProgress()
    try:
        return await _write_and_hold(
            context, job_id, language, result, llm, scope, settings
        )
    finally:
        try:
            await release_whatsapp(tenant, job_id, language)
        except JobStoreUnavailable:
            # The claim's own expiry frees it; the answer stands either way.
            pass


async def _write_and_hold(
    context: RequestContext,
    job_id: str,
    language: str,
    result: dict[str, object],
    llm: LLMClient,
    scope: TenantScope,
    settings: Settings,
) -> WhatsAppSuggestion:
    """The pass, then the suggestion held for as long as its call is."""
    tenant = context.tenant
    config = await resolve_calls_config(tenant)
    transcript = Transcript.model_validate(result["transcript"])
    call = CallText.of(
        transcript,
        country_code=config.phone_country_code,
        spoken=spoken_of(result.get("languages")),
    )
    written = await write_whatsapp(
        routed(llm, config.model_route),
        call,
        language,
        scope=scope,
        settings=settings,
    )
    suggestion: dict[str, object] = {
        "language": language,
        "dialect": language if language in ARABIC_LANGUAGES else None,
        "text": written.whatsapp,
        "versions": {"prompt": PROMPT_SET_VERSION},
    }
    try:
        left = await result_seconds_left(tenant, job_id)
        if left is not None:
            ttl = min(left, config.result_ttl_seconds)
            await store_whatsapp(tenant, job_id, language, suggestion, ttl_seconds=ttl)
    except JobStoreUnavailable:
        # Paid for and answered; only its hold failed. Logged, never re-asked.
        left = None
    _logger.info(
        "call_whatsapp_written",
        extra={
            "tenant": tenant,
            "request_id": context.request_id,
            "job_id": job_id,
            "language": language,
            "held": left is not None,
        },
    )
    return _suggestion(job_id, suggestion)
