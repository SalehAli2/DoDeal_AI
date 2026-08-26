"""Per-tenant backend credential resolution (audit finding F2).

The backend's contract is one DD-API-KEY per tenant: a key is valid only
against its own tenant host, so a single shared key cannot serve more than one
tenant, and there is no safe fallback if a tenant's key is missing. Sending
another tenant's key, or a placeholder, to a real backend host is exactly the
kind of mistake that must fail loudly before it fails silently as a 401 (or,
worse, succeeds against the wrong tenant).

The lookup is a swappable seam (TenantKeyResolver Protocol), mirroring how
TokenVerifier lets Gate 1's verification mechanism change without touching
callers. SettingsKeyResolver is today's implementation, reading the map parsed
from DODEAL_DD_API_KEYS. A secret-manager-backed resolver (fetching a key by
tenant from a vault at request time instead of from process env) is a second
implementation of the same Protocol — callers never change.

The key is read in exactly ONE place in src/ (tools/leads.py, where the header
is built) via SecretStr.get_secret_value(). Everywhere else — including the
failure path here — the key never appears: an unknown tenant fails closed with
a fixed reason code and the tenant name only, never key material from any
tenant's entry.
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from pydantic import SecretStr

from dodeal_ai.core.config import Settings, get_settings

_logger = logging.getLogger("dodeal_ai.tools")


class BackendKeyError(Exception):
    """No backend key is configured for this tenant. Fail closed: raised
    before any network call is attempted, never retried, never wrapped by the
    watchdog. str() is the fixed reason code alone -- no key material, no map
    contents, from this tenant or any other."""

    def __init__(self, tenant: str):
        self.reason_code = "backend_key_missing"
        self.tenant = tenant
        super().__init__(self.reason_code)

    def __str__(self) -> str:
        return self.reason_code


@runtime_checkable
class TenantKeyResolver(Protocol):
    """The swap seam. An implementation takes a tenant subdomain and returns
    that tenant's backend key, or raises BackendKeyError. Runtime-checkable
    (same as core/llm/client.py's LLMClient) so conformance can be asserted
    with isinstance() in tests without a real backend/vault resolver existing
    yet to import."""

    def resolve(self, tenant: str) -> SecretStr: ...


class SettingsKeyResolver:
    """Today's implementation: looks up the tenant in Settings.dd_api_keys
    (parsed from DODEAL_DD_API_KEYS)."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def resolve(self, tenant: str) -> SecretStr:
        try:
            return self._settings.dd_api_keys[tenant]
        except KeyError:
            _logger.error("backend_key_missing tenant=%s", tenant)
            raise BackendKeyError(tenant) from None


def get_key_resolver() -> TenantKeyResolver:
    """FastAPI dependency, same shape as get_verifier(): tests override this
    at the route, never by patching a module global."""
    return SettingsKeyResolver()
