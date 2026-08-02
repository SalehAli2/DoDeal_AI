"""The ONE place that maps our internal names to actual JWT claim keys.

Internal names (tenant_id, subject, roles) are what the rest of the service
speaks. The wire claim keys the backend actually sends are UNCONFIRMED
(tenant_id/sub/roles vs org_id/user_id/role). This module is the sole reader of
the claim-name config in core/config.py, so when the backend sends real sample
JSON, exactly one thing changes (the DODEAL_CLAIM_* env vars / config defaults)
and nothing else in the codebase moves.

No gate, no other module, ever reaches into a raw claim dict by key name. They
call extract_identity() and get back internal names only.
"""
from __future__ import annotations

from dataclasses import dataclass

from dodeal_ai.core.config import Settings, get_settings


class ClaimMappingError(Exception):
    """A required claim was absent or the wrong shape in the token payload.

    Raised with a short, non-sensitive reason code — never the claim value.
    Gate 1 turns this into a generic 401; the reason code is for the audit line,
    not the client.
    """

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class Identity:
    """Internal-name view of a token's identity claims. Intermediate result of
    the mapping step — RequestContext (Step 3) is the richer downstream object.
    """

    tenant_id: str
    subject: str
    roles: tuple[str, ...]


def _require_str(payload: dict, key: str, reason_code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ClaimMappingError(reason_code)
    return value


def _require_roles(payload: dict, key: str, reason_code: str) -> tuple[str, ...]:
    value = payload.get(key)
    # Absent roles -> empty tuple is a legitimate "authenticated but no roles"
    # state; authz (Gate 3) is what turns that into a deny. Missing is fine;
    # wrong *shape* (not a list of strings) is a mapping error.
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(r, str) for r in value):
        raise ClaimMappingError(reason_code)
    return tuple(value)


def extract_identity(payload: dict, settings: Settings | None = None) -> Identity:
    """Read internal identity from a verified token payload via the configured
    claim-name mapping. `settings` is injectable for tests; defaults to the
    shared cached Settings.

    Reason codes are deliberately generic ('missing_tenant', etc.) — safe to log,
    reveal nothing to the client.
    """
    settings = settings or get_settings()
    tenant_id = _require_str(payload, settings.claim_tenant_id, "missing_tenant")
    subject = _require_str(payload, settings.claim_subject, "missing_subject")
    roles = _require_roles(payload, settings.claim_roles, "malformed_roles")
    return Identity(tenant_id=tenant_id, subject=subject, roles=roles)