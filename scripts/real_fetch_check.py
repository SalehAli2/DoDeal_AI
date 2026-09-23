"""Manual, one-shot real-backend check for LeadsClient.

Makes ONE real HTTP call to the live backend to verify the lead integration
end to end, for the joint verification session with the backend team. This is
NOT a pytest test: it hits the real network, needs a real per-tenant
DD-API-KEY, and must never run in CI or the hermetic suite. It is not
discovered by pytest at all (no test_ filename, lives under scripts/).

Safety: the real, credentialed call to production is opt-in, never a default.
Without --live, this DRY-RUNS against a fake, guaranteed-unreachable host
(leads-check.invalid) and makes NO real network call. Add --live to make the
real call:

Usage:
    uv run python scripts/real_fetch_check.py <tenant-subdomain>
    # or, with the tenant set via env var instead of a CLI argument:
    DODEAL_CHECK_TENANT=<tenant-subdomain> uv run python scripts/real_fetch_check.py

    # the real, credentialed call:
    uv run python scripts/real_fetch_check.py <tenant-subdomain> --live

Requires:
    - DODEAL_DD_API_KEYS set to a JSON map that includes this tenant's real,
      per-tenant key (via .env or the environment), e.g.
      DODEAL_DD_API_KEYS={"<tenant-subdomain>":"<key>"}. Never hardcode a key
      here, and never a tenant name either: the tenant comes ONLY from the
      CLI argument or DODEAL_CHECK_TENANT, so there is no default this can
      be pointed at by accident. Only read when --live is passed.
    - DODEAL_JWT_SIGNING_KEY set to anything. Settings requires it to fail
      closed, even though this script bypasses the gate chain entirely and
      never touches a JWT. Your local .env already has this if you can run
      the app at all.

What it prints (register item 89): the status and a fixed diagnosis for a
refused call -- never a response body, which is the CRM's and may quote the
request -- the exception TYPE for a network failure, never its message, and
lead ids on success, never a name.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import httpx
from pydantic import SecretStr

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.tools.errors import (
    BackendEnvelopeInvalid,
    BackendError,
    BackendStatusError,
)
from dodeal_ai.tools.httpx_transport import HttpxTransport
from dodeal_ai.tools.keys import SettingsKeyResolver, TenantKeyResolver
from dodeal_ai.tools.leads import LeadsClient

# RFC 2606 reserves .invalid and guarantees it is never resolvable in DNS.
# This is the fake, guaranteed-unreachable host used for dry runs.
_DRY_RUN_HOST = "leads-check.invalid"


def _parse_args() -> tuple[str, bool]:
    parser = argparse.ArgumentParser(
        description="Manual, one-shot real-backend check for LeadsClient."
    )
    parser.add_argument(
        "tenant", nargs="?", default=None, help="Tenant subdomain (positional)."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "REQUIRED to make a real call to the live backend "
            "(https://<tenant>.dodealcrm.com). Without this flag, the script "
            f"dry-runs the same logic against a fake, guaranteed-unreachable "
            f"host ({_DRY_RUN_HOST}) and makes NO real network call."
        ),
    )
    args = parser.parse_args()

    tenant = (args.tenant or "").strip()
    if tenant:
        return tenant, args.live
    tenant = os.environ.get("DODEAL_CHECK_TENANT", "").strip()
    if tenant:
        return tenant, args.live
    print(
        "Usage: uv run python scripts/real_fetch_check.py <tenant-subdomain>\n"
        "       (or set DODEAL_CHECK_TENANT=<tenant-subdomain> instead)\n"
        "       Add --live to make a real call; without it, this dry-runs.",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _build_scope(tenant: str) -> TenantScope:
    # Minimal scope: get_leads only reads .tenant. Everything else is a
    # placeholder -- this is a direct tool-layer call, not a gated request, so
    # there is no real subject or request_id to carry.
    #
    # A TenantScope, not a RequestContext, since the tool layer took the
    # narrower type (design note 0001, D1). Constructed directly here because
    # this script has no gate chain to narrow a context FROM -- which is
    # exactly the non-user-principal case D1 describes. That is also why the
    # "one construction site" test greps src/ only.
    return TenantScope(
        tenant=tenant,
        subject="manual-script",
        database="",
        request_id="manual-real-fetch-check",
    )


def _dry_run_settings(real_settings: Settings) -> Settings:
    """A settings object identical to the real one, except pointed at the
    fake unreachable dry-run host. The .invalid TLD never resolves, so no
    bytes -- credentials included -- ever leave the machine. The dry run
    never touches settings.dd_api_keys at all: _DryRunKeyResolver supplies a
    fixed dummy key so the tenant does not need a real one configured just to
    exercise the request/error-handling logic."""
    return real_settings.model_copy(update={"backend_base_domain": _DRY_RUN_HOST})


class _DryRunKeyResolver:
    """Always resolves to a fixed dummy key, for any tenant. Used only in
    dry-run mode, where no real key is needed and no real network call
    happens."""

    def resolve(self, tenant: str) -> SecretStr:
        return SecretStr("dry-run-no-key")


def _guard_against_accidental_live_call(settings: Settings, live: bool) -> None:
    """Defense in depth: refuse outright to dispatch any request whose
    target host is dodealcrm.com unless --live was explicitly passed, even
    if the dry-run wiring above has a bug. The real credentialed call to
    production must be a deliberate action, never a side effect of testing."""
    if not live and "dodealcrm.com" in settings.backend_base_domain:
        print(
            "REFUSING: backend_base_domain resolves to a *.dodealcrm.com host "
            "and --live was not passed. This would be a real call to the live "
            "backend. Pass --live to confirm you intend to do that.",
            file=sys.stderr,
        )
        raise SystemExit(3)


def _mask_key(key: str) -> str:
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


# How many lead ids the success line prints.
_IDS_SHOWN = 5


def _report_external_call_error(exc: ExternalCallError) -> None:
    """The status and a fixed diagnosis; never the body, never a message."""
    cause = exc.cause
    if isinstance(cause, BackendStatusError):
        status = cause.status
        if status in (401, 403):
            print(
                f"AUTH PROBLEM: backend returned {status}. Check that "
                "this tenant's entry in DODEAL_DD_API_KEYS is the correct, "
                "currently-valid key, and that the tenant subdomain is right.",
                file=sys.stderr,
            )
        elif status == 422:
            print(
                "REQUEST REJECTED (422): the backend did not accept the "
                "request as sent. Check the tenant subdomain / URL shape.",
                file=sys.stderr,
            )
        else:
            print(f"BACKEND HTTP ERROR: {status}", file=sys.stderr)
    else:
        print(
            f"NETWORK PROBLEM: could not complete the request "
            f"({type(cause).__name__}). Check connectivity, DNS, "
            "and that the tenant subdomain is reachable.",
            file=sys.stderr,
        )


def _report_validation_error(exc: BackendEnvelopeInvalid) -> None:
    print(
        f"SHAPE MISMATCH: the response for '{exc.label}' did not match the "
        "expected schema (dodeal_ai.schemas.lead.LeadListResponse). The real backend "
        "response differs from the confirmed spec in ASSUMPTIONS.md -- bring "
        "this back to the joint session.",
        file=sys.stderr,
    )
    print(
        "Validation errors (field path and problem only; no field values shown):",
        file=sys.stderr,
    )
    for loc, error_type in exc.errors:
        print(f"  - {loc or '(root)'}: {error_type}", file=sys.stderr)


async def _run(tenant: str, live: bool) -> int:
    real_settings = get_settings()
    key_resolver: TenantKeyResolver

    if live:
        settings = real_settings
        if tenant not in settings.dd_api_keys:
            print(
                f"No key configured for tenant {tenant!r} in DODEAL_DD_API_KEYS.\n"
                "Set it to the real per-tenant key before running with --live.",
                file=sys.stderr,
            )
            return 1
        key_resolver = SettingsKeyResolver(settings)
    else:
        settings = _dry_run_settings(real_settings)
        key_resolver = _DryRunKeyResolver()

    _guard_against_accidental_live_call(settings, live)

    scope = _build_scope(tenant)
    # Built from the same two settings LeadsClient._base_url reads, scheme
    # included: a printed URL that disagrees with the call it describes is
    # worse than no printed URL at all.
    url = (
        f"{settings.backend_scheme}://{tenant}."
        f"{settings.backend_base_domain}/api/service/leads"
    )
    mode = (
        "LIVE (real call to production)" if live else "DRY RUN (no real network call)"
    )

    print(f"Mode:       {mode}")
    print(f"Tenant:     {tenant}")
    print(f"Fetching:   GET {url}")
    if live:
        print(
            f"DD-API-KEY: {_mask_key(key_resolver.resolve(tenant).get_secret_value())}"
        )
    print()

    try:
        # One client for the one call, with the service's own timeout.
        async with httpx.AsyncClient(
            timeout=settings.external_call_timeout_seconds
        ) as http:
            client = LeadsClient(HttpxTransport(http), key_resolver, settings)
            leads = await client.get_leads(scope)
    except BackendError as exc:
        print(f"FAILED: the backend refused the call ({exc.reason_code}).")
        return 1
    except ExternalCallError as exc:
        _report_external_call_error(exc)
        if not live:
            print(
                "\n(This failure is EXPECTED in dry-run mode -- the host is "
                "deliberately unreachable. The script's request/error-handling "
                "logic ran correctly. Pass --live to make the real call.)"
            )
            return 0
        return 1
    except BackendEnvelopeInvalid as exc:
        _report_validation_error(exc)
        return 1

    print(f"OK: parsed {len(leads)} lead(s) against the confirmed schema.")
    if leads:
        # Ids only: a lead's name is a real client's, and this is a terminal.
        shown = ", ".join(str(lead.id) for lead in leads[:_IDS_SHOWN])
        print(f"First lead ids: {shown}")
    else:
        print("(This tenant currently has zero leads -- shape still parsed cleanly.)")
    return 0


def main() -> int:
    tenant, live = _parse_args()
    return asyncio.run(_run(tenant, live))


if __name__ == "__main__":
    raise SystemExit(main())
