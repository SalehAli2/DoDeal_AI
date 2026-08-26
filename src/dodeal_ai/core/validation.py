"""Response validation — every model/tool output checked against a schema
before it is used or returned.

An LLM can hallucinate a wrong shape; a backend response can drift. We validate
against a Pydantic schema (the versioned contracts in schemas/) at the boundary.
On failure we REJECT and fail closed — malformed output is never surfaced to the
caller or fed downstream.

The rejected content never leaves this module. Pydantic's ValidationError
carries the offending value (`input_value`) in both its str() and its errors(),
so it is neither stored on our exception nor chained to it: a chained cause is
printed by anything that formats a traceback, which would put raw note text or
model output into a log line. We keep only the dotted field location and
pydantic's error TYPE — both fixed vocabulary. See ASSUMPTIONS §3.3.

Reusable: pass any schema and any raw dict. Nothing feature-specific here.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

_logger = logging.getLogger("dodeal_ai.validation")


class OutputValidationError(Exception):
    """A model/tool output did not match its expected schema. Fail closed:
    raise rather than returning partial/coerced data.

    Carries a short label (which output failed), how many problems there were,
    and one (dotted location, pydantic error type) pair per problem — e.g.
    ("data.0.note", "string_type"). It does NOT carry the ValidationError, the
    pydantic messages, or the input values: those contain the rejected content
    itself, which could be a note body or a model completion. This exception is
    expected to reach a log line, so everything on it must be safe there.
    """

    def __init__(self, label: str, errors: tuple[tuple[str, str], ...]):
        self.label = label
        self.errors = errors
        self.error_count = len(errors)
        super().__init__(f"output failed validation: {label}")


def validate_output[M: BaseModel](schema: type[M], raw: object, *, label: str) -> M:
    """Validate `raw` against `schema`, returning the parsed model on success.

    - schema: a Pydantic model class (e.g. schemas.lead_v1.LeadV1).
    - raw: the untrusted output (usually a dict from an LLM or a tool response).
    - label: short name for logs (e.g. "tool.get_lead", "llm.unit_a").

    Raises OutputValidationError on any mismatch. The raw content is NOT logged
    and is NOT attached to the raised error.
    """
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        # include_input=False is what keeps the rejected value out; the loc and
        # the type are pydantic's own vocabulary and carry no caller content.
        details = tuple(
            (".".join(str(part) for part in item["loc"]), item["type"])
            for item in exc.errors(
                include_url=False, include_input=False, include_context=False
            )
        )
        # Log that validation failed, how many problems, and which KINDS — but
        # not the raw data itself. The counts and types are safe; content is not.
        _logger.warning(
            "output_validation_failed label=%s error_count=%d error_types=%s",
            label,
            len(details),
            ",".join(dict.fromkeys(error_type for _, error_type in details)),
        )
        # from None: chaining the ValidationError would carry input_value into
        # any traceback formatted downstream. Deliberate — do not "restore" it.
        raise OutputValidationError(label, details) from None
