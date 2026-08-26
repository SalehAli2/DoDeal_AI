"""Structured audit logging — one single-line JSON record per auth decision.

Rules (from the spec, non-negotiable):
  - allow -> INFO, deny -> WARNING.
  - Deny events are NEVER dropped or sampled.
  - No secrets, no token contents, no raw claims. Only: decision, tenant,
    request_id, reason_code, and the gate that decided.
  - One line, valid JSON, so a log collector can parse it.

What is safe to log and why: tenant and request_id are identifiers, not
secrets; reason_code is a fixed vocabulary we defined (never a claim value or a
token fragment). Everything else stays out.

The fields travel as `extra=`, which is logging's own structured-field
mechanism; core/logging_config.py's JsonFormatter merges them at the top level
of the JSON line. They are NOT pre-serialised into the message string: a
formatter that unpacks JSON-looking messages would let any other log line whose
text happens to start with "{" forge these same fields.
"""

from __future__ import annotations

import logging
from typing import Literal

_logger = logging.getLogger("dodeal_ai.audit")

Decision = Literal["allow", "deny"]


def audit(
    *,
    decision: Decision,
    gate: str,
    request_id: str,
    reason_code: str,
    tenant: str | None = None,
) -> None:
    """Emit one structured audit line.

    tenant is optional because Gate 1 failures happen BEFORE we have a
    trustworthy tenant — we must not invent one. When it's unknown we log null,
    never a guessed or header-supplied value.
    """
    fields = {
        "event": "auth_decision",
        "decision": decision,
        "gate": gate,
        "tenant": tenant,
        "request_id": request_id,
        "reason_code": reason_code,
    }
    if decision == "deny":
        _logger.warning("auth_decision", extra=fields)  # denies never dropped
    else:
        _logger.info("auth_decision", extra=fields)
