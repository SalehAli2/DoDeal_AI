"""Manual, one-shot real-provider check for the LLM seam.

Makes REAL, PAID model calls to verify the adapter end to end: the request
shape, JSON mode, the status and finish-reason maps, the token counts, and the
model string the provider reports. This is NOT a pytest test -- it hits the
network, spends money, and must never run in CI or the hermetic suite. It is
not discovered by pytest at all (no test_ filename, lives under scripts/).

Safety, in the shape of scripts/real_fetch_check.py: the paid call is opt-in
and never a default. Without --live this DRY-RUNS against a fake,
guaranteed-unreachable host (model-smoke.invalid) and makes no real call.

THE NOTE TEXT IS INVENTED AND HARDCODED BELOW. Nothing from the real export,
no real tenant, no real salesperson. A real note must never reach a provider
until the residency question (Q20) has an answer.

Usage:
    uv run python scripts/model_smoke.py                  # dry run
    uv run python scripts/model_smoke.py --live           # REAL, PAID calls
    uv run python scripts/model_smoke.py --induce-timeout # no key needed

Requires for --live:
    - DODEAL_LLM_PROVIDER, DODEAL_LLM_MODEL and DODEAL_LLM_API_KEY, via .env or
      the environment. The key is never printed; only its last four characters.
    - DODEAL_JWT_SIGNING_KEY set to anything. Settings requires it to fail
      closed, even though this script bypasses the gate chain entirely.

--induce-timeout needs NO key and NO provider account. It stands up a local
socket that accepts a connection and never answers, points the adapter at it,
and proves the thing httpx.MockTransport structurally cannot: that
_HTTP_TIMEOUT_SHARE makes httpx's own timer fire before the watchdog's, so a
timeout arrives named rather than as a bare TimeoutError. See the report block
for Piece 76.1a.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import socket
import sys
import time

import httpx
from pydantic import SecretStr

from dodeal_ai.core.config import ConfigError, Settings, get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.errors import ModelUnavailableError
from dodeal_ai.core.llm import build_llm_client
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_CLASSIFY
from dodeal_ai.schemas.lead import Lead, LeadNote
from dodeal_ai.units.structured_intelligence.classify import classify

# RFC 2606 reserves .invalid and guarantees it never resolves in DNS.
_DRY_RUN_HOST = "https://model-smoke.invalid/v1"

# INVENTED. Shaped like a real note -- a callback with a next step and a number
# the classifier has to not invent -- and belonging to nobody.
_NOTE_TEXT = (
    "Called the client back about the two-bedroom unit in the north tower. "
    "He is still interested but wants to see the payment plan in writing "
    "before committing. Agreed I would send it over tomorrow morning and "
    "call again on Thursday to confirm he received it."
)

_TENANT = "smoke-tenant"


def _scope() -> TenantScope:
    # A TenantScope, not a RequestContext: this is a direct unit call with no
    # gate chain to narrow a context from (design note 0001, D1), the same
    # non-user-principal case scripts/real_fetch_check.py constructs one for.
    return TenantScope(
        tenant=_TENANT,
        subject="model-smoke",
        database="",
        request_id="manual-model-smoke",
    )


def _note() -> LeadNote:
    return LeadNote(
        id=1,
        note=_NOTE_TEXT,
        author="Invented Salesperson",
        author_id=1,
        createdAt="2026-09-12T09:00:00Z",
    )


def _lead() -> Lead:
    """INVENTED, like the note. The four fields the classifier's context
    section reads, and nothing that belongs to anybody."""
    return Lead(
        id=1,
        name="Invented Buyer",
        leadType="buyer",
        enquiryType="residential",
        project="North Tower",
        status="active",
    )


def _mask(key: str) -> str:
    return "*" * max(len(key) - 4, 0) + key[-4:] if len(key) > 4 else "*" * len(key)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Manual, one-shot real-provider check for the LLM seam."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "REQUIRED to make real, PAID model calls. Without it this dry-runs "
            f"the same logic against {_DRY_RUN_HOST} and calls nothing."
        ),
    )
    parser.add_argument(
        "--induce-timeout",
        action="store_true",
        help=(
            "Point the adapter at a local socket that accepts and never "
            "answers, to prove the timeout path end to end. Needs no key."
        ),
    )
    return parser.parse_args()


# --- the induced timeout ----------------------------------------------------


class _BlackHole:
    """A socket that accepts a connection and then says nothing, ever.

    Not a mock: a real TCP listener, because the thing under test is whether
    httpx's own read timer fires before the watchdog's, and httpx.MockTransport
    does not enforce timeouts at all -- the handler simply runs to completion.
    Only a real socket that goes quiet can produce a real ReadTimeout.

    The backlog is accepted and the connection HELD, never closed: a closed
    socket is a transport error, which takes a different branch and would prove
    nothing about the timer.
    """

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self._held: list[socket.socket] = []
        self._stop = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._sock.getsockname()[1]}/v1"

    def serve_forever(self) -> None:
        self._sock.settimeout(0.5)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            self._held.append(conn)  # accepted, never answered

    def close(self) -> None:
        self._stop = True
        for conn in self._held:
            with contextlib.suppress(OSError):
                conn.close()
        with contextlib.suppress(OSError):
            self._sock.close()


class _Capture(logging.Handler):
    """Every dodeal_ai record, kept as (message, fields) so the report can say
    what fired and what it carried."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def find(self, message: str) -> logging.LogRecord | None:
        return next((r for r in self.records if r.getMessage() == message), None)


async def _run_induced_timeout() -> int:
    hole = _BlackHole()
    loop = asyncio.get_running_loop()
    server = loop.run_in_executor(None, hole.serve_forever)

    capture = _Capture()
    logger = logging.getLogger("dodeal_ai")
    logger.addHandler(capture)
    original_level = logger.level
    logger.setLevel(logging.DEBUG)

    try:
        base = get_settings()
        settings = base.model_copy(
            update={
                "llm_provider": base.llm_provider or _groq(),
                "llm_model": "smoke-induced-timeout",
                "llm_api_key": SecretStr("dummy-not-a-real-key"),
                "llm_base_url": hole.base_url,
                "llm_timeout_seconds": 5.0,
            }
        )
        print("Mode:        INDUCED TIMEOUT (no provider account, no key)")
        print(f"Base URL:    {hole.base_url}  (accepts, never answers)")
        print(f"Timeout:     {settings.llm_timeout_seconds}s watchdog")
        print(
            f"HTTP budget: {settings.llm_timeout_seconds * _share():.2f}s "
            f"(_HTTP_TIMEOUT_SHARE={_share()})"
        )
        print()

        async with httpx.AsyncClient() as http:
            client = build_llm_client(settings, http)
            started = time.monotonic()
            outcome = "NO EXCEPTION -- that is a defect"
            try:
                await classify(
                    client, _note(), _lead(), scope=_scope(), settings=settings
                )
            except ModelUnavailableError as exc:
                outcome = f"{type(exc).__name__} ({exc.http_status} {exc.reason_code})"
            elapsed = time.monotonic() - started

        print(f"Outcome:     {outcome}")
        print(f"Elapsed:     {elapsed:.2f}s")
        print()
        _report_timeout_evidence(capture)
        return 0
    finally:
        logger.removeHandler(capture)
        logger.setLevel(original_level)
        hole.close()
        await server


def _report_timeout_evidence(capture: _Capture) -> None:
    """The four questions this mode exists to answer."""
    adapter = capture.find("llm_provider_call_failed")
    outcome = capture.find("judgement_model_unavailable")

    print("EVIDENCE")
    print(f"  llm_provider_call_failed fired : {adapter is not None}")
    if adapter is not None:
        print(f"    provider  : {getattr(adapter, 'provider', '<<missing>>')}")
        print(f"    reason    : {getattr(adapter, 'reason_code', '<<missing>>')}")
        print(f"    status    : {getattr(adapter, 'status', '<<missing>>')}")
        print(f"    transient : {getattr(adapter, 'transient', '<<missing>>')}")
    print(f"  judgement_model_unavailable    : {outcome is not None}")
    if outcome is not None:
        print(
            f"    provider_reason    : "
            f"{getattr(outcome, 'provider_reason', '<<missing>>')}"
        )
        print(
            f"    provider_transient : "
            f"{getattr(outcome, 'provider_transient', '<<missing>>')}"
        )
        print(
            f"    error_type         : {getattr(outcome, 'error_type', '<<missing>>')}"
        )
    print()
    print(
        "NOTE: the provider label is expected to be 'openai_compatible', not\n"
        "'groq', because DODEAL_LLM_BASE_URL is an override and an unrecognised\n"
        "base URL is labelled generically on purpose -- a proxy URL's host or\n"
        "path can itself be a credential. That is designed behaviour."
    )


def _groq():
    from dodeal_ai.core.config import LLMProvider

    return LLMProvider.GROQ


def _share() -> float:
    from dodeal_ai.core.llm.openai_compatible import _HTTP_TIMEOUT_SHARE

    return _HTTP_TIMEOUT_SHARE


# --- the real call ----------------------------------------------------------


def _dry_run_settings(real: Settings) -> Settings:
    """The real settings, pointed at an unresolvable host, with a dummy key. No
    bytes -- credentials included -- leave the machine."""
    return real.model_copy(
        update={
            "llm_provider": real.llm_provider or _groq(),
            "llm_model": real.llm_model or "dry-run-model",
            "llm_api_key": SecretStr("dry-run-no-key"),
            "llm_base_url": _DRY_RUN_HOST,
        }
    )


async def _run_call(settings: Settings, live: bool) -> int:
    mode = "LIVE (real, PAID model calls)" if live else "DRY RUN (no real call)"
    print(f"Mode:        {mode}")
    print(f"Provider:    {settings.llm_provider}")
    print(f"Model asked: {settings.llm_model}")
    print(f"Base URL:    {settings.llm_base_url or '(vendor default)'}")
    if live and settings.llm_api_key is not None:
        print(f"API key:     {_mask(settings.llm_api_key.get_secret_value())}")
    print(f"Note:        invented, {len(_NOTE_TEXT)} chars")
    print()

    async with httpx.AsyncClient() as http:
        client = build_llm_client(settings, http)

        # Connect + send, measured separately: the first real number behind
        # _HTTP_TIMEOUT_SHARE, which is a judgement and not a measurement.
        connect_started = time.monotonic()
        try:
            started = time.monotonic()
            parsed, response = await classify(
                client, _note(), _lead(), scope=_scope(), settings=settings
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
        except ModelUnavailableError as exc:
            if not live:
                print(
                    f"{type(exc).__name__} ({exc.reason_code}) -- EXPECTED in dry "
                    "run: the host is deliberately unresolvable. The request and "
                    "error-handling path ran correctly. Pass --live for the real "
                    "call."
                )
                return 0
            print(f"FAILED: {type(exc).__name__} ({exc.reason_code})", file=sys.stderr)
            print(
                "Check DODEAL_LLM_API_KEY, the model id, and the provider's "
                "status page. Nothing about the failure is printed here; the "
                "adapter logs the provider and status and never the body.",
                file=sys.stderr,
            )
            return 1

    print(f"OK: one pass ({PROFILE_UNIT_A_CLASSIFY}) in {elapsed_ms} ms")
    print(f"  note_type        : {parsed.note_type}")
    print(f"  model REPORTED   : {response.model}")
    print(f"  finish_reason    : {response.finish_reason.value}")
    print(f"  tokens in/out    : {response.input_tokens} / {response.output_tokens}")
    print(f"  request id       : {response.provider_request_id}")
    print(f"  connect+send+read: {int((time.monotonic() - connect_started) * 1000)} ms")
    print()
    print(
        "This is ONE pass. The full three-pass judgement runs through the "
        "pipeline, which needs Redis for its reservation and counters; this "
        "script deliberately exercises the SEAM, not the unit's state layer."
    )
    return 0


async def _main() -> int:
    args = _parse_args()

    try:
        real = get_settings()
    except ConfigError as exc:
        print(f"CONFIG: {exc}", file=sys.stderr)
        print(
            "DODEAL_JWT_SIGNING_KEY must be set even here -- Settings fails "
            "closed without it, though this script never touches a JWT.",
            file=sys.stderr,
        )
        return 2

    if args.induce_timeout:
        return await _run_induced_timeout()

    if not args.live:
        return await _run_call(_dry_run_settings(real), live=False)

    # The named error the brief asks for, so this is safe to invoke in CI: a
    # missing key is exit 1 with a reason, never a traceback and never a call.
    if real.llm_provider is None or not real.llm_model:
        print(
            "NOT CONFIGURED: set DODEAL_LLM_PROVIDER and DODEAL_LLM_MODEL "
            "before --live.",
            file=sys.stderr,
        )
        return 1
    if real.llm_api_key is None:
        print(
            "NO API KEY: set DODEAL_LLM_API_KEY before --live. Refusing to "
            "make an unauthenticated call that would 401 at cost.",
            file=sys.stderr,
        )
        return 1
    return await _run_call(real, live=True)


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
