"""One-command local end-to-end run of a real call (Unit B), for the lead.

    uv run python -m scripts.call_e2e <audio> [--language en|ar|mixed]
        [--tenant tenant-a] [--stt-profile NAME] [--no-stage2]
        [--out DIR] [--timeout 1800]

Starts the API, the normal and stage-2 call workers and call_demo.py's
file-and-callback server as child processes, switches calls on for the
tenant, pushes the call as the CRM would, follows the job, prints a readable
report and saves everything to --out. Demo settings reach the children
through their environment only; .env is never written. Refuses production,
any path inside this repository, and a start while other workers already
consume the call queues (they would take the job with their own settings). The recording goes to the configured
speech-to-text provider: use only calls the audio-governance answer allows.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import httpx
import jwt
import redis
from arq.constants import health_check_key_suffix

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.redis import redis_url as store_url
from dodeal_ai.units.call_intelligence.queues import (
    NORMAL_QUEUE,
    OVERNIGHT_QUEUE,
    PRIORITY_QUEUE,
    STAGE2_QUEUE,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_ENV = REPO_ROOT / ".env.demo"
DOT_ENV = REPO_ROOT / ".env"

TERMINAL = frozenset({"done", "failed", "dead_letter"})
STAGE2_TERMINAL = frozenset({"done", "failed", "not_eligible"})
POLL_SECONDS = 2.0
TOKEN_LIFETIME_SECONDS = 240
TEXT_LIMIT = 400

EXIT_DONE, EXIT_FAILED, EXIT_TIMEOUT, EXIT_INTERRUPTED = 0, 1, 2, 130

# Every call queue a running worker could take this run's jobs from.
CALL_QUEUES = (PRIORITY_QUEUE, NORMAL_QUEUE, OVERNIGHT_QUEUE, STAGE2_QUEUE)


# --- small helpers --------------------------------------------------------------


def fail(message: str) -> SystemExit:
    return SystemExit(f"call_e2e: {message}")


def outside_repo(path: Path) -> Path:
    """`path` resolved, or exit when it is inside this repository."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise fail("a path inside this repository was refused")
    return resolved


def plain(value: object) -> str:
    """A setting as text: a SecretStr's value, an enum's value, else str()."""
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter())
    return str(getattr(value, "value", value))


def env_file_values(path: Path) -> dict[str, str]:
    """KEY=VALUE lines of an env file; comments and blanks skipped."""
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        values[name.strip()] = value.strip()
    return values


def busy_queues(client: Any) -> list[str]:
    """The call queues another worker consumes: those whose arq health-check
    key exists. arq deletes it on a clean stop; a killed worker's expires
    within its health-check interval plus a second (3601 s by default)."""
    return [
        queue for queue in CALL_QUEUES if client.exists(queue + health_check_key_suffix)
    ]


def refuse_other_workers(client: Any) -> None:
    """Exit, naming them, while other workers consume the call queues."""
    busy = busy_queues(client)
    if busy:
        raise fail(
            f"refused: other workers are consuming {', '.join(busy)} "
            "(arq health-check keys present). Stop them first; a killed "
            "worker's key expires within an hour."
        )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def audio_seconds(audio: Path) -> float:
    """The recording's length from ffprobe."""
    try:
        done = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(audio),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(done.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError):
        raise fail("ffprobe could not read the recording's length") from None


def find(node: Any, key: str) -> Any:
    """The first value under `key` anywhere in a JSON tree, else None."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        for value in node.values():
            found = find(value, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = find(value, key)
            if found is not None:
                return found
    return None


def at(node: Any, *keys: str) -> Any:
    """The value at an exact path of dict keys, else None."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def whole(value: object) -> str:
    """A value in full, never cut: indented JSON for a dict or a list."""
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def part_reasons(outcomes: list[dict[str, Any]]) -> Any:
    """part_reasons from the last call_stage2_outcome line, else None."""
    lines = [r for r in outcomes if r.get("message") == "call_stage2_outcome"]
    return lines[-1].get("part_reasons") if lines else None


def short(value: object) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= TEXT_LIMIT else text[:TEXT_LIMIT] + " ..."


# --- the child processes ------------------------------------------------------------


class Children:
    """The API, the workers and the demo server; every log to its own file."""

    def __init__(self, logs: Path, env: dict[str, str]) -> None:
        self.logs = logs
        self.env = env
        self.procs: list[tuple[str, subprocess.Popen[bytes], IO[bytes]]] = []

    def start(self, name: str, argv: list[str]) -> Path:
        path = self.logs / f"{name}.log"
        handle = path.open("wb")
        proc = subprocess.Popen(
            argv, cwd=REPO_ROOT, env=self.env, stdout=handle, stderr=subprocess.STDOUT
        )
        self.procs.append((name, proc, handle))
        return path

    def dead(self) -> list[str]:
        return [name for name, proc, _ in self.procs if proc.poll() is not None]

    def stop(self) -> None:
        for _, proc, _ in reversed(self.procs):
            if proc.poll() is None:
                proc.terminate()
        for _, proc, handle in self.procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            handle.close()


def wait_for_http(url: str, seconds: float) -> httpx.Response | None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            response = httpx.get(url, timeout=2)
            if response.status_code < 500:
                return response
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    return None


def served_audio_url(log: Path, seconds: float) -> str | None:
    """The audio URL call_demo.py prints in its push example."""
    pattern = re.compile(r'"audio_url":\s*"([^"]+)"')
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if log.exists():
            match = pattern.search(log.read_text(encoding="utf-8", errors="replace"))
            if match:
                return match.group(1)
        time.sleep(0.5)
    return None


# --- the report ----------------------------------------------------------------------


def outcome_records(log: Path, name: str) -> list[dict[str, Any]]:
    """The JSON log lines of a worker that carry `name`."""
    found: list[dict[str, Any]] = []
    if not log.exists():
        return found
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if name not in line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            found.append(record)
    return found


def report(body: dict[str, Any], outcomes: list[dict[str, Any]]) -> list[str]:
    """A readable report of the status body and the outcome lines."""
    lines = [
        "=== CALL ===",
        f"status: {body.get('status')}  reason: {body.get('reason')}",
        f"delivery: {body.get('delivery')}  stage2: {body.get('stage2')}",
        f"analysis_reason: {at(body, 'result', 'analysis_reason')}",
        f"part_reasons: {whole(part_reasons(outcomes))}",
        f"stage2_result.reasons: {whole(at(body, 'stage2_result', 'reasons'))}",
        "",
        "=== TRANSCRIPT ===",
        f"language profile: {find(body, 'language_profile')}",
        (
            f"uncertain: {find(body, 'uncertain')}  "
            f"reasons: {find(body, 'uncertain_reasons')}"
        ),
        (
            f"segments: {len(find(body, 'segments') or [])}  "
            f"engine: {find(body, 'provider')} / {find(body, 'model')}"
        ),
        f"roles: {short(find(body, 'roles'))}",
        "",
        "=== WAVE 1 ===",
        f"summary: {short(find(body, 'summary'))}",
        f"crm note: {short(find(body, 'crm_note'))}",
    ]
    details = find(body, "details") or {}
    if isinstance(details, dict):
        for name, detail in details.items():
            if isinstance(detail, dict):
                lines.append(
                    f"  {name}: {short(detail.get('value'))} [{detail.get('state')}]"
                )
    lines += [
        f"mood: {short(find(body, 'mood'))}",
        "",
        "=== SIGNALS ===",
        f"talk balance: {find(body, 'talk_balance')}",
        f"agent: {short(find(body, 'agent'))}",
        f"client: {short(find(body, 'client'))}",
        f"numbers: {short(find(body, 'numbers'))}",
        f"alarms: {short(find(body, 'alarms'))}",
        f"escalations: {short(find(body, 'escalations'))}",
        f"keywords: {short(find(body, 'keywords'))}",
        "",
        "=== WAVE 2 ===",
        f"objections: {short(find(body, 'objections'))}",
        f"score (LOCAL TEST, never for a person): {short(find(body, 'score'))}",
        f"coaching: {short(find(body, 'observations'))}",
        f"plan: {short(find(body, 'plan'))}",
        "",
        "=== EXTRAS ===",
    ]
    extras = at(body, "stage2_result", "extras")
    if isinstance(extras, dict):
        for name in ("whatsapp_suggestion", "seriousness", "tags", "keywords"):
            lines.append(f"{name}: {whole(extras.get(name))}")
    else:
        lines.append(f"extras: {extras}")
    lines += [
        "",
        "=== TOKENS AND COST ===",
    ]
    total = 0.0
    for record in outcomes:
        cost = record.get("cost_usd")
        if isinstance(cost, int | float):
            total += float(cost)
        spend = {k: v for k, v in record.items() if "token" in k or "cost" in k}
        lines.append(f"  {record.get('message', record.get('event'))}: {short(spend)}")
    lines.append(f"total cost_usd: {round(total, 6)}")
    return lines


# --- the run -------------------------------------------------------------------------


def finished(body: dict[str, Any], want_stage2: bool) -> bool:
    status = body.get("status")
    if status not in TERMINAL:
        return False
    if status != "done" or not want_stage2:
        return True
    stage2 = body.get("stage2")
    return stage2 is None or stage2 in STAGE2_TERMINAL


def run(args: argparse.Namespace) -> int:
    audio = outside_repo(args.audio)
    if not audio.is_file():
        raise fail("the recording was not found")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = outside_repo(args.out or audio.parent / f"e2e_{audio.stem}_{stamp}")
    (out / "logs").mkdir(parents=True, exist_ok=True)

    settings = get_settings()
    environment = os.environ.get("DODEAL_ENVIRONMENT") or plain(
        getattr(settings, "environment", "development")
    )
    if environment.lower() == "production":
        raise fail("refused: DODEAL_ENVIRONMENT is production")
    provider = plain(getattr(settings, "call_stt_provider", "fake"))
    if provider == "fake" and not args.stt_profile:
        raise fail(
            "the fake STT is refused: set DODEAL_CALL_STT_PROVIDER or use --stt-profile"
        )
    try:
        redis_url = plain(settings.redis_operational_url)
        redis.Redis.from_url(redis_url, socket_connect_timeout=2).ping()
    except (redis.RedisError, AttributeError, ValueError):
        raise fail("Redis did not answer. Run: docker compose up -d redis") from None
    try:
        queue = redis.Redis.from_url(store_url("queue"), socket_connect_timeout=2)
        refuse_other_workers(queue)
    except (redis.RedisError, ValueError):
        raise fail("the queue Redis did not answer") from None

    key = env_file_values(DEMO_ENV).get("DODEAL_SERVICE_JWT_SIGNING_KEY")
    if not key:
        raise fail("no DODEAL_SERVICE_JWT_SIGNING_KEY in .env.demo")
    issuer = plain(getattr(settings, "service_jwt_issuer", "dodeal-crm"))
    audience = plain(getattr(settings, "service_jwt_audience", "dodeal-ai"))
    base_domain = plain(getattr(settings, "inbound_base_domain", "dodealcrm.com"))
    host = f"{args.tenant}.{base_domain}"
    callback_secret = secrets.token_hex(16)
    seconds = audio_seconds(audio)

    env = {**env_file_values(DOT_ENV), **os.environ}
    env.update(
        {
            "DODEAL_TENANT_CONFIG_CACHE_SECONDS": "1",
            "DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO": "true",
            "DODEAL_CALL_CALLBACK_SECRETS": json.dumps({args.tenant: callback_secret}),
            "DODEAL_SERVICE_JWT_ALGORITHM": "HS256",
            "DODEAL_SERVICE_JWT_SIGNING_KEY": key,
            "PYTHONUNBUFFERED": "1",
        }
    )
    api_port, demo_port = free_port(), free_port()
    api = f"http://127.0.0.1:{api_port}"
    python = sys.executable
    children = Children(out / "logs", env)

    def token() -> str:
        now = int(time.time())
        claims = {
            "iss": issuer,
            "aud": audience,
            "subdomain": args.tenant,
            "iat": now,
            "exp": now + TOKEN_LIFETIME_SECONDS,
        }
        return jwt.encode(claims, key, algorithm="HS256")

    def headers() -> dict[str, str]:
        return {"Host": host, "Authorization": f"Bearer {token()}"}

    try:
        children.start(
            "api",
            [
                python,
                "-m",
                "uvicorn",
                "dodeal_ai.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
        )
        worker_logs = [
            children.start(
                "worker-normal", [python, "-m", "dodeal_ai.workers.calls", "normal"]
            )
        ]
        if not args.no_stage2:
            worker_logs.append(
                children.start(
                    "worker-stage2", [python, "-m", "dodeal_ai.workers.calls", "stage2"]
                )
            )
        demo_log = children.start(
            "demo",
            [
                python,
                str(REPO_ROOT / "scripts" / "call_demo.py"),
                "--port",
                str(demo_port),
                "--tenant",
                args.tenant,
                "--secret",
                callback_secret,
                "--audio",
                str(audio),
                "--base-url",
                api,
            ],
        )

        if wait_for_http(f"{api}/health", 60) is None:
            raise fail(f"the API did not start; see {out / 'logs' / 'api.log'}")
        ready = httpx.get(f"{api}/ready", timeout=5)
        print(f"api ready: {ready.status_code} {short(ready.text)}")
        time.sleep(3)
        if children.dead():
            raise fail(f"child exited at start: {children.dead()}; see {out / 'logs'}")

        audio_url = served_audio_url(demo_log, 20)
        if audio_url is None:
            raise fail(f"the demo server printed no audio URL; see {demo_log}")

        section: dict[str, Any] = {
            "calls_enabled": True,
            "audio_hosts": ["127.0.0.1"],
            "callback_url": f"http://127.0.0.1:{demo_port}/callback",
            "number_detection_enabled": True,
            "alarm_phrases_enabled": True,
            "scoring_enabled": True,
        }
        if args.stt_profile:
            section["stt_profile"] = args.stt_profile
        with httpx.Client(base_url=api, timeout=30) as client:
            put = client.put(
                "/api/v1/admin/tenant-config/unit_b", json=section, headers=headers()
            )
            print(f"unit_b settings: {put.status_code}")
            if put.status_code >= 300:
                raise fail(f"the settings PUT failed: {short(put.text)}")
            time.sleep(3)
            now = datetime.now(UTC)
            push = {
                "call_id": int(time.time()) % 1_000_000_000,
                "lead_id": 1004,
                "author_id": 7,
                "duration_seconds": max(1, round(seconds)),
                "recorded_at": now.isoformat(timespec="seconds"),
                "audio_url": audio_url,
                "audio_url_expires_at": (now + timedelta(hours=1)).isoformat(
                    timespec="seconds"
                ),
                "language_hint": args.language,
            }
            started = time.monotonic()
            pushed = client.post("/api/v1/calls/jobs", json=push, headers=headers())
            if pushed.status_code != 202:
                raise fail(
                    f"the push failed: {pushed.status_code} {short(pushed.text)}"
                )
            job_id = pushed.json()["job_id"]
            print(f"pushed call, job {job_id} ({round(seconds)} s of audio)")

            body: dict[str, Any] = {}
            seen: tuple[object, ...] = ()
            code = EXIT_TIMEOUT
            while time.monotonic() - started < args.timeout:
                got = client.get(f"/api/v1/calls/jobs/{job_id}", headers=headers())
                if got.status_code == 200:
                    body = got.json()
                    state = (
                        body.get("status"),
                        body.get("delivery"),
                        body.get("stage2"),
                    )
                    if state != seen:
                        elapsed = round(time.monotonic() - started, 1)
                        print(
                            f"[{elapsed:>7} s] status={state[0]} "
                            f"delivery={state[1]} stage2={state[2]}"
                        )
                        seen = state
                    if finished(body, not args.no_stage2):
                        code = (
                            EXIT_DONE if body.get("status") == "done" else EXIT_FAILED
                        )
                        break
                if children.dead():
                    print(f"a child exited: {children.dead()}; see {out / 'logs'}")
                    code = EXIT_FAILED
                    break
                time.sleep(POLL_SECONDS)

        time.sleep(2)
        outcomes = [
            record
            for log in worker_logs
            for name in ("call_job_outcome", "call_stage2_outcome")
            for record in outcome_records(log, name)
        ]
        (out / "status.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out / "outcomes.json").write_text(
            json.dumps(outcomes, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        callbacks = [
            line
            for line in demo_log.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if "call." in line
        ]
        text = "\n".join(
            [*report(body, outcomes), "", "=== CALLBACKS (demo server) ===", *callbacks]
        )
        (out / "report.txt").write_text(text, encoding="utf-8")
        print(text)
        print(f"\nsaved to {out}")
        if code == EXIT_TIMEOUT:
            print(f"timed out after {args.timeout} s")
        return code
    except KeyboardInterrupt:
        print("interrupted; stopping")
        return EXIT_INTERRUPTED
    finally:
        children.stop()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", type=Path)
    parser.add_argument("--language", choices=["en", "ar", "mixed"], default="mixed")
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--stt-profile", default="")
    parser.add_argument("--no-stage2", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=1800.0)
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
