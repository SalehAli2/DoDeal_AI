"""Gate 3 — role/permission authorization.

An endpoint declares the ONE permission it requires. We resolve the caller's
roles (from the verified Identity) into a permission set via a role->permission
table, then check the required permission against it. Default deny: if the
permission isn't explicitly granted, the request is refused (403). There is no
implicit allow, no wildcard, no "admin bypasses everything" here.

The ROLE TABLE below is a PLACEHOLDER — agent and viewer only — pending Product
sign-off on the real role names and grants (Q4: we heard ~9 roles, exact names
unconfirmed). It lives in ONE place; when the real table lands, this dict (or a
config/fetch source) is the single thing that changes.
"""

from __future__ import annotations

from dodeal_ai.core.auth.claims import Identity
from dodeal_ai.core.context import RequestContext


class PermissionDeniedError(Exception):
    """Caller lacked the required permission. -> 403. Reason code is audit-safe
    and generic; it names the gate outcome, not the caller's business."""

    def __init__(self, reason_code: str = "permission_denied"):
        self.reason_code = reason_code
        super().__init__(reason_code)


# --- PLACEHOLDER role->permission table (UNCONFIRMED, pending Product) --------
# agent  : the people who write lead notes (the Phase 1 users).
# viewer : read-only.
# Real role names/grants replace this ONE dict when Product signs off.
#
# Read permissions only. The service has NO write path to the CRM
# (ASSUMPTIONS §3.1: judgements are returned to the caller, which persists what
# it chooses), so a note:write grant here would name a capability that does not
# exist -- and default-deny is a weaker claim when the table grants things
# nothing can do. It goes in the day a write endpoint does.
_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "agent": frozenset({"lead:read", "note:read"}),
    "viewer": frozenset({"lead:read", "note:read"}),
}


def resolve_permissions(identity: Identity) -> frozenset[str]:
    """Union the permissions granted by each of the caller's roles. Unknown
    roles contribute nothing (they are not an error here — they simply grant no
    permissions, and default-deny does the rest)."""
    granted: set[str] = set()
    for role in identity.roles:
        granted |= _ROLE_PERMISSIONS.get(role, frozenset())
    return frozenset(granted)


def require_permission(context: RequestContext, permission: str) -> None:
    """Enforce that the context holds the required permission. Raises
    PermissionDeniedError (default deny) if not. Returns None on success."""
    if not context.has_permission(permission):
        raise PermissionDeniedError()
