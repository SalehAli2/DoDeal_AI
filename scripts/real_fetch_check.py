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
    DODEAL_CHECK_TENANT=nasir3 uv run python scripts/real_fetch_check.py

    # the real, credentialed call:
    uv run python scripts/real_fetch_check.py <tenant-subdomain> --live

Requires:
    - DODEAL_DD_API_KEY set to a real, per-tenant key (via .env or the
      environment). Never hardcode it here. Only read when --live is passed.
    - DODEAL_JWT_SIGNING_KEY set to anything. Settings requires it to fail
      closed, even though this script bypasses the gate chain entirely and
      never touches a JWT. Your local .env already has this if you can run
      the app at all.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# schemas/ is a plain top-level directory, not part of the installed
# dodeal_ai package. pytest gets it on sys.path via pythonpath = ["."] in
# pyproject.toml; a bare script invocation does not, so add the repo root
# here, before importing anything that transitively imports schemas.lead.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.tools.httpx_transport import HttpxTransport
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


def _build_context(tenant: str) -> RequestContext:
    # Minimal context: get_leads only reads .tenant. Everything else is a
    # placeholder -- this is a direct tool-layer call, not a gated request,
    # so there is no real subject/roles/permissions/request_id to carry.
    return RequestContext(
        tenant=tenant,
        subject="manual-script",
        database="",
        roles=(),
        permissions=frozenset(),
        request_id="manual-real-fetch-check",
    )


def _dry_run_settings(real_settings: Settings) -> Settings:
    """A settings object identical to the real one, except pointed at the
    fake unreachable dry-run host and with the key blanked out. The .invalid
    TLD never resolves, so no bytes -- credentials included -- ever leave
    the machine; blanking the key here is defense in depth on top of that."""
    return real_settings.model_copy(
        update={"backend_base_domain": _DRY_RUN_HOST, "dd_api_key": "dry-run-no-key"}
    )


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


def _report_external_call_error(exc: ExternalCallError) -> None:
    cause = exc.cause
    if isinstance(cause, httpx.HTTPStatusError):
        status = cause.response.status_code
        snippet = cause.response.text[:300]
        if status in (401, 403):
            print(
                f"AUTH PROBLEM: backend returned {status}. Check that "
                "DODEAL_DD_API_KEY is the correct, currently-valid key for "
                "this tenant, and that the tenant subdomain is right.",
                file=sys.stderr,
            )
        elif status == 422:
            print(
                "REQUEST REJECTED (422): the backend did not accept the "
                "request as sent. Check the tenant subdomain / URL shape.",
                file=sys.stderr,
            )
        else:
            print(
                f"BACKEND HTTP ERROR: {status} {cause.response.reason_phrase}",
                file=sys.stderr,
            )
        print(f"Response body (truncated): {snippet}", file=sys.stderr)
    else:
        print(
            f"NETWORK PROBLEM: could not complete the request "
            f"({type(cause).__name__}: {cause}). Check connectivity, DNS, "
            "and that the tenant subdomain is reachable.",
            file=sys.stderr,
        )


def _report_validation_error(exc: OutputValidationError) -> None:
    print(
        f"SHAPE MISMATCH: the response for '{exc.label}' did not match the "
        "expected schema (schemas.lead.LeadListResponse). The real backend "
        "response differs from the confirmed spec in ASSUMPTIONS.md -- bring "
        "this back to the joint session.",
        file=sys.stderr,
    )
    print(
        "Validation errors (field path and problem only; no field values shown):",
        file=sys.stderr,
    )
    for error in exc.cause.errors(include_url=False):
        loc = ".".join(str(part) for part in error["loc"])
        print(
            f"  - {loc or '(root)'}: {error['type']} - {error['msg']}", file=sys.stderr
        )


async def _run(tenant: str, live: bool) -> int:
    real_settings = get_settings()

    if live:
        settings = real_settings
        if not settings.dd_api_key or settings.dd_api_key == "test-dd-api-key":
            print(
                "DODEAL_DD_API_KEY is not set (still the placeholder default).\n"
                "Set it to the real per-tenant key before running with --live.",
                file=sys.stderr,
            )
            return 1
    else:
        settings = _dry_run_settings(real_settings)

    _guard_against_accidental_live_call(settings, live)

    context = _build_context(tenant)
    client = LeadsClient(HttpxTransport(), settings)
    url = f"https://{tenant}.{settings.backend_base_domain}/api/service/leads"
    mode = (
        "LIVE (real call to production)" if live else "DRY RUN (no real network call)"
    )

    print(f"Mode:       {mode}")
    print(f"Tenant:     {tenant}")
    print(f"Fetching:   GET {url}")
    if live:
        print(f"DD-API-KEY: {_mask_key(settings.dd_api_key)}")
    print()

    try:
        leads = await client.get_leads(context)
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
    except OutputValidationError as exc:
        _report_validation_error(exc)
        return 1

    print(f"OK: parsed {len(leads)} lead(s) against the confirmed schema.")
    if leads:
        first = leads[0]
        print(f"First lead: id={first.id} name={first.name!r}")
    else:
        print("(This tenant currently has zero leads -- shape still parsed cleanly.)")
    return 0


def main() -> int:
    tenant, live = _parse_args()
    return asyncio.run(_run(tenant, live))


if __name__ == "__main__":
    raise SystemExit(main())
