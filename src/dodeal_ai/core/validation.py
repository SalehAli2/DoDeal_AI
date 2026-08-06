"""Response validation — every model/tool output checked against a schema
before it is used or returned.

An LLM can hallucinate a wrong shape; a backend response can drift. We validate
against a Pydantic schema (the versioned contracts in schemas/) at the boundary.
On failure we REJECT and fail closed — malformed output is never surfaced to the
caller or fed downstream.

Reusable: pass any schema and any raw dict. Nothing feature-specific here.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, ValidationError

_logger = logging.getLogger("dodeal_ai.validation")


class OutputValidationError(Exception):
    """A model/tool output did not match its expected schema. Fail closed:
    raise rather than returning partial/coerced data. Carries a short label
    (which output failed) for logs — never the raw invalid content, which could
    contain unexpected or sensitive material."""

    def __init__(self, label: str, cause: ValidationError):
        self.label = label
        self.cause = cause
        super().__init__(f"output failed validation: {label}")


def validate_output[M: BaseModel](schema: type[M], raw: object, *, label: str) -> M:
    """Validate `raw` against `schema`, returning the parsed model on success.

    - schema: a Pydantic model class (e.g. schemas.lead_v1.LeadV1).
    - raw: the untrusted output (usually a dict from an LLM or a tool response).
    - label: short name for logs (e.g. "tool.get_lead", "llm.unit_a").

    Raises OutputValidationError on any mismatch. The raw content is NOT logged.
    """
    try:
        return schema.model_validate(raw)
    except ValidationError as exc:
        # Log that validation failed and how many problems — but not the raw
        # data itself. error_count is safe; the content is not.
        _logger.warning(
            "output_validation_failed label=%s error_count=%d",
            label,
            exc.error_count(),
        )
        raise OutputValidationError(label, exc) from exc
