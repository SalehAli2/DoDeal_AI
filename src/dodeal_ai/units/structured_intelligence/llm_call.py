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
the reprompt Phase G adds would have to land in all three. So the shape lives
here from the first call site.

WHAT IT DOES NOT DO:

  - It does not retry. `retry=False` on every model call, always: a paid call
    that may already have completed is never repeated, and the fail policy for a
    model failure is an enumerated error, not a second attempt.
  - It does not repair. A response wrapped in a code fence, missing a field, or
    carrying an extra one is MALFORMED, not something to clean up. Repairing it
    in code would mean the judgement was partly ours; Phase G's single reprompt
    is the one recovery there is.
  - It does not log the output. Not the text, not the decoder's message, not the
    pydantic message — only the label, the counts and the fixed error types that
    `validate_output` already produces.

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
from dodeal_ai.core.llm import LLMClient, LLMResponse
from dodeal_ai.core.log_safety import safe_error_fields
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.core.resilience import ExternalCallError, call_with_watchdog
from dodeal_ai.core.validation import OutputValidationError, validate_output

_logger = logging.getLogger("dodeal_ai.unit_a")


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
    client: LLMClient, prompt: AssembledPrompt, label: str, *, settings: Settings
) -> LLMResponse:
    """Send one prompt. No retry, the per-call LLM timeout, never the global.

    Every failure the provider or the network can produce -- a translated
    `LLMProviderError`, a timeout, a transport error -- arrives here as
    `ExternalCallError` and leaves as `ModelUnavailableError` (503
    `model_unavailable`). One code for "the model did not answer", because the
    caller can do exactly one thing about any of them: try again later.
    """

    async def _send() -> LLMResponse:
        return await client.complete(prompt)

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

    Raises `OutputValidationError` for all three: text that is not JSON at all,
    JSON that is not the shape we asked for, and a well-shaped answer that
    breaks a rule depending on the note type or the tenant. One exception type,
    because Phase G's reprompt treats them identically -- the model was told
    what to answer, and every one of these means it did not.

    `check` raises; it never returns a repaired value. A hook that could rewrite
    the answer would put part of the judgement in code that no prompt test
    covers.

    The decoder's message is dropped and the exception is unchained. A
    `JSONDecodeError` carries the offending document on `.doc`, and formatting
    it anywhere would put model output -- which may quote the note -- into a log
    line.
    """
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
    check: Callable[[M], None] | None = None,
) -> tuple[M, LLMResponse]:
    """Send one prompt and return the validated output beside the raw response.

    The response comes back too, and not just the parsed model, for one reason:
    `LLMResponse.model` is what the provider REPORTED it ran, and that string is
    stamped on the judgement as `model_version`. A helper that returned only the
    parsed object would leave the caller with nothing to stamp.

    ONE call, ONE validation, and a second failure is not possible here because
    there is no first recovery yet: a malformed response becomes
    `MalformedOutputError` (503 `malformed_output`) immediately. PHASE G inserts
    the single reprompt between the failure and that error -- rebuilding the
    prompt with `AssembledPrompt.tail` and calling once more. The call sites do
    not change when it does.
    """
    response = await complete_once(client, prompt, label, settings=settings)
    try:
        return parse_output(response, schema, label, check=check), response
    except OutputValidationError:
        # from None: the OutputValidationError is ours and safe, but chaining it
        # would print a second exception line wherever a traceback is formatted,
        # and the rule in this repo is that our error paths stay unchained.
        raise MalformedOutputError() from None
