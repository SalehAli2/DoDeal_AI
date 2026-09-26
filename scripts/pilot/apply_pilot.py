"""Apply DoDeal's pilot settings: validate both sections, refuse a placeholder,
then PUT each through the admin route.

    uv run python scripts/pilot/apply_pilot.py --dry-run
    DODEAL_PILOT_SERVICE_TOKEN=... uv run python scripts/pilot/apply_pilot.py \\
        --base-url https://<the service>

WHAT IT CHECKS, before anything is sent: each body is a JSON object that the
service's own section parser accepts (the same parser the admin PUT runs), and
no string anywhere in either body contains PLACEHOLDER, in any case. A refusal
names the section and the field path, never a value. Nothing is PUT unless
both bodies pass, so a refusal changes nothing on the service.

--dry-run only validates: it prints what is left to fill and sends nothing.

It runs with the service's environment (DODEAL_*): the parsers read the
deployment's model routes and STT profiles from it. The service token comes
from DODEAL_PILOT_SERVICE_TOKEN and is never printed. No PUT is retried: a
failure is reported with its status code and the script stops, naming which
section, if any, was already applied. The admin PUT replaces the whole
section, and both stamps are the service's.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

from dodeal_ai.core.config import get_settings
from dodeal_ai.units.call_intelligence.config import parse_unit_b_section
from dodeal_ai.units.structured_intelligence.config import parse_unit_a_section

HERE = Path(__file__).resolve().parent
TOKEN_ENV = "DODEAL_PILOT_SERVICE_TOKEN"
MARKER = "placeholder"
# Section -> the admin route it is PUT to (api/routes/admin.py).
ROUTES = {
    "unit_a": "/api/v1/admin/tenant-config",
    "unit_b": "/api/v1/admin/tenant-config/unit_b",
}
# The unit_a parser requires a config_version, which the service stamps on a
# PUT whatever the body says; this one only lets the local check run.
_CHECK_VERSION = "pilot-local-check"
# One admin PUT is small and cheap; a slow service is a failure, not a wait.
_TIMEOUT_SECONDS = 30.0


class Refused(Exception):
    """A body that must not be sent. The message names paths, never values."""


def load(path: Path, section: str) -> dict:
    """One body: a JSON object, or Refused naming the section."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Refused(f"{section}: not a readable JSON file") from None
    if not isinstance(body, dict):
        raise Refused(f"{section}: not a JSON object")
    return body


def placeholders(value: object, path: str) -> list[str]:
    """Every field path under `value` whose string contains the marker."""
    if isinstance(value, str):
        return [path] if MARKER in value.casefold() else []
    if isinstance(value, dict):
        return [
            found
            for key, item in value.items()
            for found in placeholders(item, f"{path}.{key}")
        ]
    if isinstance(value, list):
        return [
            found
            for index, item in enumerate(value)
            for found in placeholders(item, f"{path}[{index}]")
        ]
    return []


def validate(bodies: dict[str, dict]) -> None:
    """Each body through its section's parser, or Refused naming the section.
    The parser's own message is dropped: it can quote what was sent."""
    parsers = {
        "unit_a": lambda body: parse_unit_a_section(
            {**body, "config_version": _CHECK_VERSION}
        ),
        "unit_b": parse_unit_b_section,
    }
    for section, body in bodies.items():
        try:
            parsers[section](body)
        except Exception:  # noqa: BLE001 - any parser failure is a refusal
            raise Refused(f"{section}: refused by the section's schema") from None


def apply(
    client: httpx.Client, bodies: dict[str, dict], *, host: str, token: str
) -> int:
    """PUT each section in order; stop at the first failure. No retries."""
    headers = {"Host": host, "Authorization": f"Bearer {token}"}
    applied: list[str] = []
    for section, body in bodies.items():
        response = client.put(ROUTES[section], json=body, headers=headers)
        if response.status_code != 200:
            print(f"{section}: refused by the service, status {response.status_code}")
            print(f"applied before it: {', '.join(applied) or 'nothing'}")
            return 1
        stamps = response.json()
        print(
            f"{section}: applied, version {stamps.get('version')}, "
            f"policy_version {stamps.get('policy_version')}"
        )
        applied.append(section)
    return 0


def run(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    bodies = {
        "unit_a": load(args.unit_a, "unit_a"),
        "unit_b": load(args.unit_b, "unit_b"),
    }
    validate(bodies)
    print("schema: both sections accepted")
    left = [
        found
        for section, body in bodies.items()
        for found in placeholders(body, section)
    ]
    if left:
        print("placeholders still to fill:")
        for path in left:
            print(f"  {path}")
        return 1
    if args.dry_run:
        print("dry run: nothing sent")
        return 0
    if not args.base_url:
        raise Refused("--base-url is required unless --dry-run")
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise Refused(f"{TOKEN_ENV} is not set")
    domain = args.inbound_domain or get_settings().inbound_base_domain
    with httpx.Client(
        base_url=args.base_url, timeout=_TIMEOUT_SECONDS, transport=transport
    ) as client:
        return apply(client, bodies, host=f"{args.tenant}.{domain}", token=token)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tenant", default="dodeal")
    parser.add_argument("--base-url", default="")
    parser.add_argument(
        "--inbound-domain",
        default="",
        help="The Host is <tenant>.<this>; default DODEAL_INBOUND_BASE_DOMAIN.",
    )
    parser.add_argument("--unit-a", type=Path, default=HERE / "dodeal_unit_a.json")
    parser.add_argument("--unit-b", type=Path, default=HERE / "dodeal_unit_b.json")
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None, transport: httpx.BaseTransport | None = None
) -> int:
    try:
        return run(parse_args(argv), transport)
    except Refused as refused:
        print(f"refused: {refused}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
