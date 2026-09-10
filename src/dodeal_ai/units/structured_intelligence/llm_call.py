"""One model call, validated — the single place raw model output is parsed.

THE BOUNDARY THIS MODULE IS. `LLMResponse.text` is untrusted: it is whatever a
model produced after reading a note a stranger wrote. Between that string and a
typed object there is exactly one path, and it is here. Nothing else in the unit
calls `json.loads` on model output, and nothing else decides what a malformed
response becomes.

WHY IT IS ONE FUNCTION AND NOT THREE. Classification, vague detection and
scoring each make a call with a different template and a different schema, and
each is otherwise identical: send, decode, validate, translate the failures.
Three copies of that would be three copies of the untrusted-parse boundary, and
the reprompt would have to land in all three. So the shape lives here.

THE REPROMPT, AND WHY IT IS NOT A RETRY. A malformed answer buys exactly one
more attempt, and that attempt is a DIFFERENT prompt: the same stable template
and the same caller data, plus a trusted tail that says, in more words than the
template does, to answer with the object and nothing else. A retry would send
the same prompt and hope; this changes the instruction, which is the only lever
there is when the provider is healthy and the answer is not. It is not the
watchdog's retry — that is off (`retry=False`) on both calls, because both are
paid.

WHAT DOES NOT GO INTO THE SECOND PROMPT: the first answer. Not quoted, not
summarised, not named. It is untrusted text that a stranger's note may have
shaped, and feeding it back would put it inside the prompt boundary this repo
exists to keep. The model is told the FORM was wrong, never what it wrote.

WHAT IT DOES NOT DO:

  - It does not retry. `retry=False` on every model call, always: a paid call
    that may already have completed is never repeated, and the fail policy for a
    model failure is an enumerated error, not a second attempt.
  - It does not repair. A response wrapped in a code fence, missing a field, or
    carrying an extra one is MALFORMED, not something to clean up. Repairing it
    in code would mean the judgement was partly ours; the single reprompt is the
    one recovery there is.
  - It does not log the output. Not the text, not the decoder's message, not the
    pydantic message — only the label, the counts and the fixed error types that
    `validate_output` already produces.
  - It does not go round twice. Two calls, then `MalformedOutputError`. There is
    no third attempt and no loop to bound.

VALIDATION RUNS OUTSIDE THE WATCHDOG, deliberately. `call_with_watchdog` wraps
ANY exception into `ExternalCallError`, so validating inside the wrapped
operation would turn a malformed response into an external-call failure and
report a healthy provider as unavailable. Only `client.complete()` goes inside.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable

from pydantic import BaseModel

from dodeal_ai.core.config import Settings
from dodeal_ai.core.errors import MalformedOutputError, ModelUnavailableError
from dodeal_ai.core.llm import FinishReason, LLMClient, LLMResponse
from dodeal_ai.core.log_safety import safe_error_fields
from dodeal_ai.core.prompting import AssembledPrompt, with_tail
from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog
from dodeal_ai.core.validation import OutputValidationError, validate_output

_logger = logging.getLogger("dodeal_ai.unit_a")

# The stricter instruction the second attempt carries. One file for all three
# tasks: what it says -- answer with the object and nothing around it -- is the
# same whichever object was asked for, and a per-task tail would be three files
# saying it three ways.
REPROMPT_TAIL_TEMPLATE = "structured_intelligence/reprompt_tail_v1.txt"


def output_rejected(
    label: str, errors: tuple[tuple[str, str], ...]
) -> OutputValidationError:
    """Log `output_validation_failed` and BUILD the error to raise.

    `validate_output` already emits this event for a schema failure. The two
    rejections that cannot go through a schema -- output that is not JSON at
    all, and the per-type / per-tenant rules the schema has no context for --
    emit it here, in the same shape, so one alert covers every way a model
    answer can be refused.

    Every `errors` entry is a (dotted location, error type) pair from a FIXED
    vocabulary: our own codes, or pydantic's. No model output, ever.

    It returns the exception rather than raising it, so the call site reads
    `raise output_rejected(...)` and the traceback starts where the rule is.
    """
    _logger.warning(
        "output_validation_failed label=%s error_count=%d error_types=%s",
        label,
        len(errors),
        ",".join(dict.fromkeys(error_type for _, error_type in errors)),
    )
    return OutputValidationError(label, errors)


async def complete_once(
    client: LLMClient,
    prompt: AssembledPrompt,
    label: str,
    *,
    settings: Settings,
    profile: str,
    max_output_tokens: int,
) -> LLMResponse:
    """Send one prompt. No retry, the per-call LLM timeout, never the global.

    Every failure the provider or the network can produce -- a translated
    `LLMProviderError`, a timeout, a transport error -- arrives here as
    `ExternalCallError` and leaves as `ModelUnavailableError` (503
    `model_unavailable`). One code for "the model did not answer", because the
    caller can do exactly one thing about any of them: try again later.

    `max_output_tokens` IS REQUIRED, and required in the keyword form, so that a
    fourth task cannot be added without someone deciding what its answer costs.
    The seam's own default would have been the easy thing to fall back on; a
    default is exactly what nobody revisits.

    `profile` is required for the same reason and names the TASK, never a model
    (core/llm/profiles.py). It is passed through UNRESOLVED: the adapter owns
    the table, and a profile may only LOWER the ceiling below, never raise it
    (ResolvedProfile.effective_max_output_tokens; report R17).

    THE SIZING RULE every caller obeys (register item 15). A ceiling is sized
    against the LONGEST ARABIC answer the task can produce, never the English
    one. Arabic runs roughly 2-3x the tokens per word that English does, so a
    ceiling that fits an English answer comfortably truncates an ordinary Arabic
    one -- and a truncated answer is MALFORMED here, so an English-sized ceiling
    does not degrade an Arabic note, it spends a reprompt on it and then 503s
    it. `Settings.llm_max_output_tokens` carries the same note and the same
    headroom; these are the per-task numbers it says tasks override it with.
    """

    async def _send() -> LLMResponse:
        return await client.complete(
            prompt, profile=profile, max_output_tokens=max_output_tokens
        )

    try:
        return await call_with_watchdog(
            _send,
            label=label,
            timeout=settings.llm_timeout_seconds,
            retry=False,
        )
    except ExternalCallError as exc:
        # safe_error_fields keeps the message of OUR exceptions and drops the
        # message of foreign ones; the cause here is usually LLMProviderError,
        # whose str() is a fixed reason code by construction.
        _logger.warning(
            "judgement_model_unavailable",
            extra={
                "reason_code": "model_unavailable",
                "label": label,
                **safe_error_fields(exc.cause),
            },
        )
        raise ModelUnavailableError() from None


def parse_output[M: BaseModel](
    response: LLMResponse,
    schema: type[M],
    label: str,
    *,
    check: Callable[[M], None] | None = None,
) -> M:
    """Decode the response as JSON, validate it against `schema`, then apply
    `check` -- the rules the schema cannot express.

    Raises `OutputValidationError` for all four: an answer the provider cut off
    at the output ceiling, text that is not JSON at all, JSON that is not the
    shape we asked for, and a well-shaped answer that breaks a rule depending on
    the note type or the tenant. One exception type, because the reprompt treats
    them identically -- the model was told what to answer, and every one of
    these means it did not.

    TRUNCATION IS CHECKED FIRST AND ON ITS OWN. `MAX_TOKENS` means the model
    stopped mid-sentence, so whatever came back is the beginning of an answer
    and not an answer -- even in the rare case where the fragment happens to
    parse and happens to satisfy the schema. Accepting one of those would stamp
    a judgement on half a reply. It earns the reprompt like any other malformed
    answer: the tail tells the model to be compact, which is the lever that
    makes the second attempt fit where the first did not.

    `check` raises; it never returns a repaired value. A hook that could rewrite
    the answer would put part of the judgement in code that no prompt test
    covers.

    The decoder's message is dropped and the exception is unchained. A
    `JSONDecodeError` carries the offending document on `.doc`, and formatting
    it anywhere would put model output -- which may quote the note -- into a log
    line.
    """
    if response.finish_reason is FinishReason.MAX_TOKENS:
        raise output_rejected(label, (("", "output_truncated"),))

    try:
        raw = json.loads(response.text)
    except ValueError:
        raise output_rejected(label, (("", "json_invalid"),)) from None

    parsed = validate_output(schema, raw, label=label)
    if check is not None:
        check(parsed)
    return parsed


async def call_model[M: BaseModel](
    client: LLMClient,
    prompt: AssembledPrompt,
    schema: type[M],
    label: str,
    *,
    settings: Settings,
    profile: str,
    max_output_tokens: int,
    check: Callable[[M], None] | None = None,
) -> tuple[M, LLMResponse]:
    """Send a prompt, validate the answer, and on a malformed one send it ONCE
    more with a stricter tail. Returns the validated output beside the raw
    response of whichever call produced it.

    The response comes back too, and not just the parsed model, for one reason:
    `LLMResponse.model` is what the provider REPORTED it ran, and that string is
    stamped on the judgement as `model_version`. A helper that returned only the
    parsed object would leave the caller with nothing to stamp. On a reprompt it
    is the SECOND response's stamp that is returned -- that is the call the
    judgement was built from.

    ONE reprompt, not a loop. Two calls, then `MalformedOutputError` (503
    `malformed_output`). A model that ignored the template and then ignored the
    tail is not going to be talked round on the third attempt, and every attempt
    is paid for by someone waiting on a note.

    ONE WRAPPER PER CALL, which is what makes the concurrent pass safe: vague
    detection and scoring each enter this function separately, so a reprompt on
    one of them re-issues that one and nothing else. There is no shared attempt
    state to get wrong.

    `check` runs on both attempts. It is part of what a valid answer means, not
    a second opinion about a valid one -- so a rule the schema cannot hold earns
    the reprompt exactly as a missing field does.

    BOTH attempts carry the SAME `profile`. The second call is the same task
    said more strictly, not a different one, so switching models between them
    would make the reprompt a second variable.
    """
    response = await complete_once(
        client,
        prompt,
        label,
        settings=settings,
        profile=profile,
        max_output_tokens=max_output_tokens,
    )
    try:
        return parse_output(response, schema, label, check=check), response
    except OutputValidationError:
        # The label and nothing else. Which output failed is operational; WHAT
        # it said is untrusted text shaped by a note we did not write.
        # `output_validation_failed` has already recorded the error types.
        _logger.warning(
            "reprompt_issued",
            extra={"reason_code": "reprompt_issued", "label": label},
        )

    second = await complete_once(
        client,
        with_tail(prompt, REPROMPT_TAIL_TEMPLATE),
        label,
        settings=settings,
        profile=profile,
        max_output_tokens=max_output_tokens,
    )
    try:
        return parse_output(second, schema, label, check=check), second
    except OutputValidationError:
        # from None: the OutputValidationError is ours and safe, but chaining it
        # would print a second exception line wherever a traceback is formatted,
        # and the rule in this repo is that our error paths stay unchained.
        raise MalformedOutputError() from None
