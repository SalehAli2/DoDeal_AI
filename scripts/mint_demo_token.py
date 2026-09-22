"""Mint one demo token and print the curl line that uses it (register item 78).

The demo has no CRM to get a token from, so it mints its own. Everything this
script needs comes from Settings -- the signing key, the algorithm, the three
claim NAMES, and the inbound base domain Gate 2 matches the Host against -- so
a token it prints cannot disagree with the service that verifies it. Nothing is
hardcoded here that core/config.py already decides.

IT MAKES NO NETWORK CALL. It signs a payload and prints text. The curl line is
printed for a person to run, never run by this script.

IT NEVER PRINTS THE SIGNING KEY, and does not mask and print it either: a demo
token is worth nothing once it expires, and the key it was signed with is worth
everything. The one place the key is read is jwt.encode, below.

EVERY DEFAULT IS INVENTED. The tenant (tenant-a) is the vendored fake-CRM
corpus's tenant; the lead and note ids are a lead in that corpus that actually
has notes. No real tenant, no real person, no real id.

WHICH ENVIRONMENT IT READS, and why it is not simply get_settings(). The demo
container is given `.env` and then `.env.demo`, and a later env file wins. A
script reading `.env` alone would therefore sign with a DIFFERENT key from the
one the service verifies with, and every minted token would fail Gate 1 with
`invalid_token` -- a failure that looks like a bug in the gates. So it builds
Settings over the SAME stack, in the same order (--env-file, default
.env.demo). A missing file in the stack is skipped, so a clean checkout with no
`.env` works.

TWO KINDS OF TOKEN (register item D1). The fetch route and the versions probe
take a USER token (sub, subdomain, database, iat, exp) signed with
DODEAL_JWT_SIGNING_KEY. The direct, history, brief, measures and config routes
take the CRM's SERVICE token (iss, aud, subdomain, iat, exp, five minutes at
most) signed with DODEAL_SERVICE_JWT_SIGNING_KEY -- HS256 only here, because
an RS256 or ES256 deployment holds the CRM's PUBLIC key and cannot mint.

Usage:
    uv run python scripts/mint_demo_token.py
    uv run python scripts/mint_demo_token.py --route direct
    uv run python scripts/mint_demo_token.py --route history
    uv run python scripts/mint_demo_token.py --route probe --ttl-seconds 60
    uv run python scripts/mint_demo_token.py --env-file .env   # non-demo

Requires:
    - DODEAL_JWT_SIGNING_KEY, from that stack or from the real environment
      (which outranks both files, exactly as it does for the service).
    - DODEAL_SERVICE_JWT_SIGNING_KEY and DODEAL_SERVICE_JWT_ALGORITHM=HS256 for
      the service routes; .env.demo carries invented ones.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import jwt

from dodeal_ai.core.auth.claims import normalise_tenant_label
from dodeal_ai.core.auth.service import service_key_text
from dodeal_ai.core.config import ConfigError, Settings, _build_settings

# THE env-file stack, in precedence order (later wins), and the ONE definition
# of it. It mirrors what Compose hands the container: docker-compose.yml
# contributes `.env` and docker-compose.demo.yml appends `.env.demo`. Pinned to
# those two files by tests/test_demo_env_stack.py -- if the orders ever diverge
# this script signs with a key the service does not verify with, and that
# surfaces as a generic 401 invalid_token that reads like a broken gate.
DEFAULT_ENV_STACK: tuple[str, str] = (".env", ".env.demo")

# The vendored corpus (tests/fixtures/fake_crm/tenant-a.json) has notes for 19
# of its 1448 leads; lead 1004 is one of them and note 115 is its newest. Named
# here as plain numbers rather than read from the fixture: scripts/ must not
# import from tests/, and a default that is occasionally stale is better than a
# coupling that is permanently wrong.
_DEMO_LEAD_ID = 1004
_DEMO_NOTE_ID = 115

# INVENTED, and shaped like a note a salesperson would actually write: a
# callback, a next step, and a number the model has to not make up. Belongs to
# nobody. Only the --route direct body carries it.
_DEMO_NOTE_TEXT = (
    "Called the client back about the two-bedroom in the north tower. Still "
    "interested, wants the payment plan in writing before committing. Sending "
    "it tomorrow morning and calling again Thursday to confirm."
)

# The four lead fields the classifier is allowed to see (LeadContext). These are
# lead 1004's, from the same corpus.
_DEMO_LEAD_CONTEXT = {
    "leadType": "Buyer",
    "enquiryType": "Plot",
    "project": None,
    "status": "On Hold",
}

# Invented, like the note: the day the history body says the note was written.
_DEMO_NOTE_CREATED_AT = "2026-03-04T09:15:00+04:00"

# An invented rep in tests/fixtures/user_directory/invented_users.json, for the
# brief and measures routes.
_DEMO_REP_ID = 501

# How long a minted service token lives: the five minutes the CRM is agreed to
# mint (register item D1), or the deployment's own maximum if that is shorter.
SERVICE_TTL_SECONDS = 300

_ROUTES = ("fetch", "direct", "history", "brief", "measures", "config", "probe")

# The routes behind the service chain; every other route takes a user token.
SERVICE_ROUTES = frozenset({"direct", "history", "brief", "measures", "config"})

# The routes that take a JSON body, and so a POST.
_POST_ROUTES = frozenset({"fetch", "direct", "history"})


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mint one demo token and print the curl line that uses it."
    )
    parser.add_argument(
        "--tenant",
        default="tenant-a",
        help="Tenant subdomain. Becomes the token's tenant claim AND the Host "
        "header, which Gate 2 requires to agree (default: tenant-a).",
    )
    parser.add_argument(
        "--subject",
        default="42",
        help="The token's subject (user id). Encoded as an integer when it "
        "looks like one, which is the shape the real CRM sends (default: 42).",
    )
    parser.add_argument(
        "--database",
        default="demo_tenant_a",
        help="The token's database claim. Carried for the tool layer; no gate "
        "reads it (default: demo_tenant_a).",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=3600,
        help="How long the token is valid. Short on purpose -- a demo token "
        "that outlives the demo is a credential nobody is tracking "
        "(default: 3600).",
    )
    parser.add_argument(
        "--route",
        choices=_ROUTES,
        default="fetch",
        help="Which curl line to print: 'fetch' the primary route (needs the "
        "fake CRM), 'direct' and 'history' the note-in-body routes, 'brief' "
        "and 'measures' a rep's figures, 'config' the tenant's rules, 'probe' "
        "the versions read. Service token for direct, history, brief, "
        "measures and config (default: fetch).",
    )
    parser.add_argument("--lead-id", type=int, default=_DEMO_LEAD_ID)
    parser.add_argument("--note-id", type=int, default=_DEMO_NOTE_ID)
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_STACK[-1],
        help="Layered on top of .env, later winning -- the same stack Compose "
        "gives the container, so this signs with the key that service "
        "verifies with. A file that does not exist is skipped "
        "(default: .env.demo).",
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Where the service is listening. The HOST HEADER is what Gate 2 "
        "checks; this is only where the TCP connection goes "
        "(default: http://localhost:8000).",
    )
    return parser.parse_args()


def _payload(args: argparse.Namespace, settings: Settings, tenant: str) -> dict:
    """The claims, under the names Settings maps them to.

    sub is an INTEGER when it looks like one: that is the shape the Tymon token
    carries, PyJWT's own sub check is disabled for exactly that reason
    (core/auth/verify.py), and claims.py normalises either to str.
    """
    now = int(time.time())
    subject: int | str = (
        int(args.subject) if args.subject.lstrip("-").isdigit() else args.subject
    )
    return {
        settings.claim_subject: subject,
        settings.claim_subdomain: tenant,
        settings.claim_database: args.database,
        # exp is the ONE claim JwtVerifier requires. iat is verified when
        # present, so it is set honestly rather than omitted.
        "iat": now,
        "exp": now + args.ttl_seconds,
    }


def service_payload(settings: Settings, tenant: str) -> dict:
    """The service token's claims: exactly the five the service requires."""
    now = int(time.time())
    lifetime = min(SERVICE_TTL_SECONDS, settings.service_jwt_max_lifetime_seconds)
    return {
        "iss": settings.service_jwt_issuer,
        "aud": settings.service_jwt_audience,
        settings.claim_subdomain: tenant,
        "iat": now,
        "exp": now + lifetime,
    }


def mint_service_token(settings: Settings, tenant: str) -> str:
    """A service token signed with the configured shared secret.

    Refuses anything but HS256 with a key: under RS256 or ES256 the settings
    hold the CRM's public key, which verifies and cannot sign.
    """
    key = settings.service_jwt_signing_key
    if key is None or settings.service_jwt_algorithm != "HS256":
        raise ConfigError("service_token_not_mintable")
    return jwt.encode(
        service_payload(settings, tenant), service_key_text(key), algorithm="HS256"
    )


def _shell_quote(value: str) -> str:
    """POSIX single-quoting, so a body containing an apostrophe cannot end the
    quoted argument early and turn the rest of the note into shell words."""
    return "'" + value.replace("'", "'\\''") + "'"


def _body(args: argparse.Namespace) -> str | None:
    if args.route not in _POST_ROUTES:
        return None
    if args.route == "fetch":
        return json.dumps({"lead_id": args.lead_id, "note_id": args.note_id})
    body: dict[str, object] = {
        "lead_id": args.lead_id,
        "note_id": args.note_id,
        "author_id": 7,
        "note_text": _DEMO_NOTE_TEXT,
        "lead": _DEMO_LEAD_CONTEXT,
    }
    if args.route == "history":
        body["note_created_at"] = _DEMO_NOTE_CREATED_AT
    return json.dumps(body)


_PATHS = {
    "fetch": "/api/v1/notes/judgements",
    "direct": "/api/v1/notes/judgements/direct",
    "history": "/api/v1/notes/judgements/history",
    "brief": f"/api/v1/briefs/rep/{_DEMO_REP_ID}",
    "measures": f"/api/v1/measures/reps/{_DEMO_REP_ID}",
    "config": "/api/v1/admin/tenant-config",
    # The versions read runs the whole gate chain and needs nothing else; the
    # old /_probe route is no longer served (register item 93).
    "probe": "/api/v1/meta/versions",
}


def _curl(args: argparse.Namespace, token: str, host: str) -> str:
    """One line, ready to paste. The Host header is the load-bearing part: the
    connection goes to --base-url, but Gate 2 only ever reads this."""
    url = f"{args.base_url.rstrip('/')}{_PATHS[args.route]}"
    parts = ["curl -sS"]
    if args.route in _POST_ROUTES:
        parts.append("-X POST")
    parts.append(_shell_quote(url))
    parts.append(f"-H {_shell_quote(f'Host: {host}')}")
    parts.append(f"-H {_shell_quote(f'Authorization: Bearer {token}')}")
    body = _body(args)
    if body is not None:
        parts.append(f"-H {_shell_quote('Content-Type: application/json')}")
        parts.append(f"-d {_shell_quote(body)}")
    return " ".join(parts)


def main() -> int:
    args = _parse_args()

    # _build_settings, not get_settings(): the cached accessor is pinned to
    # `.env` alone, and this must read the demo's stack. Going through it
    # rather than calling Settings() directly keeps the fail-closed
    # ConfigError conversion in the ONE place that owns it.
    try:
        settings = _build_settings(_env_file=(*DEFAULT_ENV_STACK[:-1], args.env_file))
    except ConfigError as exc:
        print(f"CONFIG: {exc}", file=sys.stderr)
        print(
            "DODEAL_JWT_SIGNING_KEY must be set -- it is the key this token is "
            f"signed with and the one the service verifies it against. It "
            f"was looked for in {', then '.join((*DEFAULT_ENV_STACK[:-1], args.env_file))}, "
            "then the environment.",
            file=sys.stderr,
        )
        return 2

    # The ONE place a tenant label is validated (core/auth/claims.py). Rejected
    # here, where it is a typo with a message, rather than in Gate 1, where it
    # is an audit line reading invalid_tenant_claim and a generic 401.
    tenant = normalise_tenant_label(args.tenant)
    if tenant is None:
        print(
            f"NOT A TENANT LABEL: {args.tenant!r}. A tenant is a single DNS "
            "label -- 1-63 characters of a-z, 0-9 and '-', no dots, no leading "
            "or trailing hyphen.",
            file=sys.stderr,
        )
        return 2

    host = f"{tenant}.{settings.inbound_base_domain}"
    if args.route in SERVICE_ROUTES:
        try:
            token = mint_service_token(settings, tenant)
        except ConfigError:
            print(
                "The service routes need DODEAL_SERVICE_JWT_ALGORITHM=HS256 and "
                "DODEAL_SERVICE_JWT_SIGNING_KEY; under RS256 or ES256 only the "
                "CRM can mint.",
                file=sys.stderr,
            )
            return 2
        claims = service_payload(settings, tenant)
        print("Token:      service (the CRM's)")
        print("Algorithm:  HS256")
        print(f"Claims:     iss, aud, {settings.claim_subdomain}, iat, exp")
        print(f"Issuer:     {claims['iss']}")
        print(f"Audience:   {claims['aud']}")
        print(f"Tenant:     {tenant}")
        print(f"Valid for:  {claims['exp'] - claims['iat']}s")
    else:
        token = jwt.encode(
            _payload(args, settings, tenant),
            settings.jwt_signing_key.get_secret_value(),
            algorithm=settings.jwt_algorithm,
        )
        print("Token:      user")
        print(f"Algorithm:  {settings.jwt_algorithm}")
        print(f"Claims:     {settings.claim_subject}, ", end="")
        print(f"{settings.claim_subdomain}, {settings.claim_database}, iat, exp")
        print(f"Tenant:     {tenant}")
        print(f"Subject:    {args.subject}")
        print(f"Valid for:  {args.ttl_seconds}s")
    print(f"Host (G2):  {host}")
    print(f"Route:      {args.route}  {_PATHS[args.route]}")
    print()
    print("TOKEN")
    print(token)
    print()
    print("CURL (the Host header is what Gate 2 checks, not the URL's host)")
    print(_curl(args, token, host))
    print()
    print(
        "PowerShell: the same line works with curl.exe rather than curl, which\n"
        "is an alias for Invoke-WebRequest there and does not take these flags."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
