"""Structured audit logging — one single-line JSON record per auth decision.

Rules (from the spec, non-negotiable):
  - allow -> INFO, deny -> WARNING.
  - Deny events are NEVER dropped or sampled.
  - No secrets, no token contents, no raw claims. Only: decision, tenant_id,
    request_id, reason_code, and the gate that decided.
  - One line, valid JSON, so a log collector can parse it.

What is safe to log and why: tenant_id and request_id are identifiers, not
secrets; reason_code is a fixed vocabulary we defined (never a claim value or a
token fragment). Everything else stays out.
"""
from __future__ import annotations

import json
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
    tenant_id: str | None = None,
) -> None:
    """Emit one structured audit line.

    tenant_id is optional because Gate 1 failures happen BEFORE we have a
    trustworthy tenant — we must not invent one. When it's unknown we log null,
    never a guessed or header-supplied value.
    """
    record = {
        "event": "auth_decision",
        "decision": decision,
        "gate": gate,
        "tenant_id": tenant_id,
        "request_id": request_id,
        "reason_code": reason_code,
    }
    line = json.dumps(record, separators=(",", ":"), sort_keys=True)
    if decision == "deny":
        _logger.warning(line)  # denies never dropped
    else:
        _logger.info(line)