"""The ONE place that maps our internal names to actual JWT claim keys.

CONFIRMED: the token is a Tymon JWT (HS256 today; RS256 requested &
agreed, awaiting provisioning). It carries:
  - sub        -> our subject (user id) — an INTEGER (e.g. 42), normalised to str
  - subdomain  -> our tenant (authoritative; Host header must also match, Gate 2)
  - database   -> the tenant's database name (carried for the tool layer later)
Plus standard iat/exp/nbf/jti/prv. NO role/permission claims.

Roles are PARKED, not removed: the permission-enforcement approach is undecided
(role-filtering behavior unknown). Identity keeps a roles field, defaulted empty,
so downstream shapes (RequestContext, Gate 3) don't break while we wait. When the
approach is confirmed, roles get sourced in ONE place — here or a backend fetch.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dodeal_ai.core.config import Settings, get_settings


class ClaimMappingError(Exception):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class Identity:
    """Internal-name view of a verified token's identity.

    tenant is the subdomain (authoritative). database is carried for the tool
    layer later. roles is always empty for now (token has none) and is kept only
    so downstream shapes don't break — see module docstring.
    """

    tenant: str
    subject: str
    database: str
    roles: tuple[str, ...] = field(default=())


def _require_str(payload: dict, key: str, reason_code: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ClaimMappingError(reason_code)
    return value


def _require_subject(payload: dict, key: str, reason_code: str) -> str:
    """sub is an INTEGER in the Tymon token (e.g. 42). Accept int or str,
    normalise to str. Reject absent/empty. Guard against bool (bool is an int
    subclass in Python, so True would otherwise slip through as 'True')."""
    value = payload.get(key)
    if value is None or value == "":
        raise ClaimMappingError(reason_code)
    if isinstance(value, bool):
        raise ClaimMappingError(reason_code)
    if isinstance(value, (int, str)):
        return str(value)
    raise ClaimMappingError(reason_code)


def extract_identity(payload: dict, settings: Settings | None = None) -> Identity:
    """Read internal identity from a verified token payload via the configured
    claim-name mapping. Reason codes are generic and audit-safe.

    roles are intentionally NOT read — the token has none. Identity.roles stays
    empty until the permission approach is confirmed.
    """
    settings = settings or get_settings()
    tenant = _require_str(payload, settings.claim_subdomain, "missing_subdomain")
    subject = _require_subject(payload, settings.claim_subject, "missing_subject")
    # database is confirmed-present but not gate-critical (it's for the tool
    # layer); default gracefully rather than failing auth if it's ever absent.
    database = payload.get(settings.claim_database, "")
    if not isinstance(database, str):
        database = ""
    return Identity(tenant=tenant, subject=subject, database=database)
