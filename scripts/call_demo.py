"""The Unit B demo on one machine: a recording to fetch, a CRM to call back,
and the lines to push a call.

WHAT IT RUNS, on http://127.0.0.1:<port>:

  GET  /demo-call.wav   a second of silence as audio/wav (or --audio's file)
  POST /callback        a stand-in CRM: verifies X-DODEAL-Signature over
                        "<timestamp>.<body>" with the demo secret, refuses a
                        timestamp over five minutes old, and prints the event,
                        the four X-DODEAL headers and the status it answered
                        -- NEVER the body, which carries the transcript

and, with --worker, the two call workers a demo call needs (demo_workers): the
normal queue's, whose transcriber is the FakeTranscriber (no speech-to-text
adapter exists yet), and the stage-2 queue's. Both build the real model client
from DODEAL_LLM_* like any call worker, so a demo call runs wave 1, sends
call.stage1, then runs wave 2 and sends call.stage2. The pushed call is 150 s,
past scoring_min_seconds, so it is eligible for stage 2.

--transcript <path> (with --worker) hands the FakeTranscriber a hand-written
transcript, so the real model runs wave 1 on words you wrote. A path inside
this repository is refused: a transcript of a real call must never land where
it could be committed. The file is JSON, one object with a list of segments,
in order and never overlapping:

    {"segments": [
      {"start_s": 0.0, "end_s": 4.5, "speaker": "agent",
       "text": "Good morning, calling about the villa.", "language": "en"},
      {"start_s": 4.8, "end_s": 9.0, "speaker": "client",
       "text": "نعم، ميزانيتي مليون درهم.", "language": "ar"}
    ]}

  start_s, end_s  seconds from the start of the call, end_s >= start_s
  speaker         "agent" is the agent; any other label is the client
  text            what was said
  language        an ISO 639 code, lower case: en, ar, ...
  confidence      optional, 0 to 1; 1.0 when left out (it was typed, not heard)

WHAT IT PRINTS: the environment the service needs (the demo flag and the
callback secret), the curl that switches calls on for the tenant, and the curl
that pushes a call. The service token for both comes from
`scripts/mint_demo_token.py --route calls`; this script mints nothing.

IT NEEDS THE SERVICE STARTED WITH DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO=true, which
admits http and loopback for audio and callbacks and logs ERROR
call_demo_audio_insecure. Demo only.

Usage:
    uv run python scripts/call_demo.py
    uv run python scripts/call_demo.py --port 8765 --secret <s> --worker
    uv run python scripts/call_demo.py --worker --transcript ~/calls/demo.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import io
import json
import secrets
import time
import wave
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

if TYPE_CHECKING:
    from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcriber

AUDIO_PATH = "/demo-call.wav"
CALLBACK_PATH = "/callback"
# The four headers a callback carries; the only ones printed.
_HEADERS = (
    "X-DODEAL-Event",
    "X-DODEAL-Event-Id",
    "X-DODEAL-Timestamp",
    "X-DODEAL-Signature",
)
# A callback older than this is refused as a replay.
MAX_AGE_SECONDS = 300

# The repository root: no --transcript may be read from under it.
REPO_ROOT = Path(__file__).resolve().parents[1]

# A typed transcript's confidence when the file gives none.
TYPED_CONFIDENCE = 1.0

# The demo call's length: past scoring_min_seconds (120 by default), so the
# call is eligible for full analysis and reaches stage 2.
DEMO_CALL_SECONDS = 150


class TranscriptRefused(ValueError):
    """A --transcript that cannot be used; str() says why, never its words."""


def load_transcript(path: Path, *, repo_root: Path = REPO_ROOT) -> tuple[Segment, ...]:
    """The segments of a hand-written transcript, checked as a transcriber's
    would be. Refused when the file sits inside the repository."""
    from pydantic import ValidationError

    from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript

    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(repo_root.resolve()):
        raise TranscriptRefused(
            "--transcript must be outside the repository: a real call's words "
            "must never be where they could be committed"
        )
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        segments = tuple(
            Segment.model_validate({"confidence": TYPED_CONFIDENCE, **segment})
            for segment in raw["segments"]
        )
        Transcript.of(segments, provider="demo", model="typed")
    except (OSError, ValueError, KeyError, TypeError, ValidationError):
        raise TranscriptRefused(
            "--transcript must be JSON with a list of segments as the docstring shows"
        ) from None
    return segments


def silent_wav(seconds: float = 1.0, rate: int = 8000) -> bytes:
    """A mono 16-bit WAV of silence: real audio/wav, and says nothing."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x00\x00" * int(seconds * rate))
    return buffer.getvalue()


def verified(secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """The receiver's check: HMAC-SHA256 of "<timestamp>.<body>", compared in
    constant time, and the timestamp no older than MAX_AGE_SECONDS."""
    try:
        age = abs(time.time() - int(timestamp))
    except ValueError:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    return age <= MAX_AGE_SECONDS and hmac.compare_digest(expected, signature)


def build_app(
    secret: str, audio: bytes, emit: Callable[[str], None] = print
) -> Starlette:
    """The recording and the stand-in CRM, on one app."""

    async def recording(request: Request) -> Response:
        return Response(audio, media_type="audio/wav")

    async def callback(request: Request) -> Response:
        body = await request.body()
        headers = {name: request.headers.get(name, "") for name in _HEADERS}
        ok = verified(
            secret, headers["X-DODEAL-Timestamp"], body, headers["X-DODEAL-Signature"]
        )
        status = 204 if ok else 401
        emit(f"callback {headers['X-DODEAL-Event'] or '(no event)'}")
        for name in _HEADERS:
            emit(f"  {name}: {headers[name]}")
        emit(f"  signature: {'verified' if ok else 'REFUSED'}  status: {status}")
        return Response(status_code=status)

    return Starlette(
        routes=[
            Route(AUDIO_PATH, recording, methods=["GET"]),
            Route(CALLBACK_PATH, callback, methods=["POST"]),
        ]
    )


def push_body(port: int, *, now: datetime) -> dict[str, object]:
    """An invented call whose recording this script serves, live for an hour."""
    now = now.replace(microsecond=0)
    return {
        "call_id": 9001,
        "lead_id": 1004,
        "author_id": 7,
        "duration_seconds": DEMO_CALL_SECONDS,
        "recorded_at": now.isoformat(),
        "audio_url": f"http://127.0.0.1:{port}{AUDIO_PATH}",
        "audio_url_expires_at": (now + timedelta(hours=1)).isoformat(),
        "language_hint": "mixed",
    }


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def instructions(
    *, port: int, tenant: str, secret: str, base_url: str, now: datetime
) -> list[str]:
    """What to set and what to run, as printed lines."""
    host = f"{tenant}.dodealcrm.com"
    config = {
        "calls_enabled": True,
        "audio_hosts": ["127.0.0.1"],
        "callback_url": f"http://127.0.0.1:{port}{CALLBACK_PATH}",
    }
    auth = "-H 'Authorization: Bearer '\"$SERVICE_TOKEN\""
    common = f"-H {_quote(f'Host: {host}')} {auth} -H 'Content-Type: application/json'"
    return [
        "1. Start the service (and the normal and stage2 workers, or --worker here)",
        "   with:",
        "   DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO=true",
        "   DODEAL_LLM_PROVIDER, DODEAL_LLM_MODEL and DODEAL_LLM_API_KEY for both waves",
        f"   DODEAL_CALL_CALLBACK_SECRETS={json.dumps({tenant: secret})}",
        "",
        "2. A service token, into $SERVICE_TOKEN:",
        "   uv run python scripts/mint_demo_token.py --route calls",
        "",
        "3. Switch calls on for the tenant:",
        (
            f"   curl -sS -X PUT {_quote(base_url + '/api/v1/admin/tenant-config/unit_b')}"
            f" {common} -d {_quote(json.dumps(config))}"
        ),
        "",
        "4. Push the call:",
        (
            f"   curl -sS -X POST {_quote(base_url + '/api/v1/calls/jobs')} {common}"
            f" -d {_quote(json.dumps(push_body(port, now=now)))}"
        ),
    ]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument(
        "--secret",
        default=None,
        help="The callback secret; a fresh one is made and printed if unset.",
    )
    parser.add_argument("--audio", type=Path, default=None)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--worker",
        action="store_true",
        help=(
            "Also run the call workers: the normal queue's with the "
            "FakeTranscriber, and the stage-2 queue's."
        ),
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=None,
        help="With --worker: a hand-written transcript, JSON, outside the repo.",
    )
    args = parser.parse_args(argv)
    if args.transcript is not None and not args.worker:
        parser.error("--transcript needs --worker: the worker is what reads it")
    return args


def demo_workers(transcriber: Transcriber) -> list[dict[str, Any]]:
    """The arq settings of the demo's call workers: the normal queue's with
    `transcriber`, and the stage-2 queue's, so a demo call reaches
    call.stage2."""
    from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, STAGE2_QUEUE
    from dodeal_ai.workers.calls import worker_settings

    return [
        worker_settings(NORMAL_QUEUE, transcriber=transcriber),
        worker_settings(STAGE2_QUEUE),
    ]


async def _serve(
    args: argparse.Namespace,
    secret: str,
    audio: bytes,
    segments: tuple[Segment, ...] | None,
) -> None:
    import uvicorn

    server = uvicorn.Server(
        uvicorn.Config(
            build_app(secret, audio),
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
        )
    )
    tasks = [server.serve()]
    if args.worker:
        from arq.worker import Worker

        from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber

        fake = FakeTranscriber() if segments is None else FakeTranscriber(segments)
        tasks += [Worker(**settings).main() for settings in demo_workers(fake)]
    await asyncio.gather(*tasks)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        segments = None if args.transcript is None else load_transcript(args.transcript)
    except TranscriptRefused as refused:
        print(refused)
        return 2
    secret = args.secret or secrets.token_hex(16)
    audio = args.audio.read_bytes() if args.audio else silent_wav()
    for line in instructions(
        port=args.port,
        tenant=args.tenant,
        secret=secret,
        base_url=args.base_url.rstrip("/"),
        now=datetime.now(UTC),
    ):
        print(line)
    print()
    print(f"Serving {AUDIO_PATH} and {CALLBACK_PATH} on 127.0.0.1:{args.port}")
    asyncio.run(_serve(args, secret, audio, segments))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
