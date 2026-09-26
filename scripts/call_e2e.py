"""One-command local end-to-end run of a real call (Unit B), for the lead.

    uv run python -m scripts.call_e2e <audio> [--language en|ar|mixed]
        [--tenant tenant-a] [--stt-profile NAME] [--no-stage2]
        [--out DIR] [--timeout 1800] [--vocabulary FILE]
        [--dialect gulf_uae|egyptian|levantine|msa]

--vocabulary is a UTF-8 file outside this repository, one term per line,
sent as the tenant's keyword_vocabulary; --dialect sets its
whatsapp_default_dialect for the run. Both go in the unit_b PUT.

Starts the API, the normal and stage-2 call workers and call_demo.py's
file-and-callback server as child processes, switches calls on for the
tenant, pushes the call as the CRM would, follows the job, prints a readable
report and saves everything to --out. Demo settings reach the children
through their environment only; .env is never written. Refuses production,
any path inside this repository, and a start while other workers already
consume the call queues (they would take the job with their own settings);
at its stop it deletes the arq health-check keys its own workers wrote, so
the next run is not refused, and never a key another worker wrote. The recording goes to the configured
speech-to-text provider: use only calls the audio-governance answer allows.
"""

from __future__ import annotations

import argparse
import html
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
from typing import IO, Any, get_args

import httpx
import jwt
import redis
from arq.constants import health_check_key_suffix

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.redis import redis_url as store_url
from dodeal_ai.units.call_intelligence.extras import WhatsAppDialect
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

# The workers this script starts: their log name, the command-line queue name
# and the arq queue each consumes -- the only health-check keys it may delete.
OWN_WORKERS = (
    ("worker-normal", "normal", NORMAL_QUEUE),
    ("worker-stage2", "stage2", STAGE2_QUEUE),
)
# How long to wait for each started worker's first health-check write.
CLAIM_SECONDS = 30.0

# The worker log lines the report reads: each run's outcome, and every model
# answer refused (label, count and error types only; never the answer).
OUTCOME_LINES = ("call_job_outcome", "call_stage2_outcome")
REJECTED_LINE = "output_validation_failed"


# --- small helpers --------------------------------------------------------------


def fail(message: str) -> SystemExit:
    return SystemExit(f"call_e2e: {message}")


def outside_repo(path: Path) -> Path:
    """`path` resolved, or exit when it is inside this repository."""
    resolved = path.resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise fail("a path inside this repository was refused")
    return resolved


def vocabulary_terms(path: Path) -> list[str]:
    """--vocabulary's terms, one per line, blank lines skipped; exit when the
    file is inside this repository, unreadable or not UTF-8 (a BOM allowed)."""
    source = outside_repo(path)
    try:
        text = source.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        raise fail("the vocabulary file could not be read as UTF-8") from None
    return [line.strip() for line in text.splitlines() if line.strip()]


def unit_b_section(
    args: argparse.Namespace, callback_url: str, vocabulary: list[str] | None
) -> dict[str, Any]:
    """The unit_b PUT: calls and every checked pass on, and what the flags set."""
    section: dict[str, Any] = {
        "calls_enabled": True,
        "audio_hosts": ["127.0.0.1"],
        "callback_url": callback_url,
        "number_detection_enabled": True,
        "alarm_phrases_enabled": True,
        "scoring_enabled": True,
    }
    if args.stt_profile:
        section["stt_profile"] = args.stt_profile
    if vocabulary is not None:
        section["keyword_vocabulary"] = vocabulary
    if args.dialect:
        section["whatsapp_default_dialect"] = args.dialect
    return section


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
    return [queue for queue in CALL_QUEUES if client.exists(health_key(queue))]


def claim_health_keys(
    client: Any,
    queues: list[str],
    seconds: float,
    claimed: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    """The health-check value each started worker wrote first, by queue. The
    keys were absent at the start (refuse_other_workers), so the first value
    seen is taken as the started worker's; a key that never appears is not
    claimed, and so never deleted. `claimed` is filled as each is seen, so a
    Ctrl+C inside the wait still leaves the stop what was claimed so far."""
    claimed = {} if claimed is None else claimed
    end = time.monotonic() + seconds
    while True:
        for queue in queues:
            value = None if queue in claimed else client.get(health_key(queue))
            if value is not None:
                claimed[queue] = value
        if len(claimed) == len(queues) or time.monotonic() >= end:
            return claimed
        time.sleep(0.5)


def release_health_keys(client: Any, claimed: dict[str, bytes]) -> list[str]:
    """After our workers stopped: delete each claimed key that still holds the
    value our worker wrote, checked and deleted in one watched transaction. A
    key another worker has written since, or one already gone, is left. The
    queues whose keys were deleted."""
    released = []
    for queue, value in claimed.items():
        with client.pipeline() as pipe:
            try:
                pipe.watch(health_key(queue))
                if pipe.get(health_key(queue)) != value:
                    continue
                pipe.multi()
                pipe.delete(health_key(queue))
                pipe.execute()
            except redis.WatchError:
                continue
        released.append(queue)
    return released


def health_key(queue: str) -> str:
    """arq's health-check key for a queue."""
    return queue + health_check_key_suffix


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


def validation_failures(outcomes: list[dict[str, Any]]) -> list[str]:
    """Every output_validation_failed line the workers logged, as logged."""
    return [
        str(record["message"])
        for record in outcomes
        if str(record.get("message", "")).startswith(REJECTED_LINE)
    ]


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
    """A readable report of the status body and the outcome lines, every
    refused model answer's line among the reasons."""
    refused = validation_failures(outcomes)
    result = at(body, "result")
    signals = at(result, "signals")
    languages = at(result, "roles", "call_languages")
    step = at(result, "analysis", "elements", "next_step")
    lines = [
        "=== CALL ===",
        f"status: {body.get('status')}  reason: {body.get('reason')}",
        f"delivery: {body.get('delivery')}  stage2: {body.get('stage2')}",
        f"analysis_reason: {at(body, 'result', 'analysis_reason')}",
        f"part_reasons: {whole(part_reasons(outcomes))}",
        f"stage2_result.reasons: {whole(at(body, 'stage2_result', 'reasons'))}",
        f"{REJECTED_LINE}: {len(refused) or 'none'}",
        *(f"  {line}" for line in refused),
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
        f"languages: {short(at(result, 'languages'))}",
        f"call_languages.client: {short(at(languages, 'client'))}",
        f"call_languages.agent: {short(at(languages, 'agent'))}",
        "",
        "=== WAVE 1 ===",
        f"summary: {short(find(body, 'summary'))}",
        f"crm note: {short(find(body, 'crm_note'))}",
        f"next step: {short(at(step, 'action'))}",
        f"  kind: {at(step, 'kind')}",
        f"  when: {at(step, 'when')} [{at(step, 'when_state')}]",
        f"  booked: {at(step, 'booked')}",
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
        (
            f"talk balance: {at(signals, 'talk_balance')}  "
            f"reason: {at(signals, 'talk_reason')}"
        ),
        f"agent talk: {short(at(signals, 'agent'))}",
        f"client talk: {short(at(signals, 'client'))}",
        f"numbers: {short(at(signals, 'numbers'))}",
        f"alarms: {short(at(signals, 'alarms'))}",
        f"escalations: {short(at(signals, 'escalations'))}",
        f"keywords: {short(at(signals, 'keywords'))}",
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
        lines += [
            f"whatsapp language: {at(extras, 'whatsapp_suggestion', 'language')}",
            f"whatsapp dialect: {extras.get('whatsapp_dialect')}",
            f"whatsapp text: {at(extras, 'whatsapp_suggestion', 'text')}",
        ]
        for name in (
            "agent_dialect",
            "seriousness",
            "tags",
            "keywords",
        ):
            lines.append(f"{name}: {whole(extras.get(name))}")
    else:
        lines.append(f"extras: {extras}")
    lines += [
        "",
        "=== TOKENS AND COST ===",
    ]
    for record in outcomes:
        if record.get("message") not in OUTCOME_LINES:
            continue
        spend = {k: v for k, v in record.items() if "token" in k or "cost" in k}
        lines.append(f"  {record.get('message', record.get('event'))}: {short(spend)}")
        lines += pass_lines(record)
    stages = [stage_cost(outcomes, name) for name in OUTCOME_LINES]
    lines += [
        f"stage 1 cost_usd: {stages[0]}",
        f"stage 2 cost_usd: {stages[1]}",
        f"total cost_usd: {summed(stages)}",
    ]
    return lines


def pass_lines(record: dict[str, Any]) -> list[str]:
    """One line per pass of an outcome line: its input, output and reasoning
    tokens side by side, and its calls; reasoning n/a where none was logged."""
    tokens = record.get("pass_tokens")
    reasoning = record.get("pass_reasoning_tokens")
    if not isinstance(tokens, dict):
        return []
    lines = []
    for name, spent in tokens.items():
        if not isinstance(spent, dict):
            continue
        thought = reasoning.get(name) if isinstance(reasoning, dict) else None
        lines.append(
            f"    {name}: input {spent.get('input')}  output {spent.get('output')}  "
            f"reasoning {'n/a' if thought is None else thought}  "
            f"calls {spent.get('calls')}"
        )
    return lines


def stage_cost(outcomes: list[dict[str, Any]], name: str) -> float | None:
    """The cost of every run of one stage, summed; None when a run had no
    price (its cost_usd null), never a partial sum that reads as the whole."""
    costs = [r.get("cost_usd") for r in outcomes if r.get("message") == name]
    return summed(costs)


def summed(costs: list[Any]) -> float | None:
    """The sum of the costs, 0 for none; None when any is not a number."""
    if not all(isinstance(cost, int | float) for cost in costs):
        return None
    return round(sum((float(cost) for cost in costs), 0.0), 6)


# --- the page --------------------------------------------------------------------

# Inline only: the page makes no network request, so it opens anywhere offline.
PAGE_STYLE = """
:root { --bg: #f6f7f9; --card: #ffffff; --ink: #1c2330; --muted: #5d6778;
  --line: #dde1e8; --agent: #dcecff; --client: #eef0f3; --other: #f4ecdf;
  --warn: #9a3412; --warn-bg: #ffedd5; --tag: #e7ecf3; }
@media (prefers-color-scheme: dark) { :root { --bg: #11151b; --card: #1a2029;
  --ink: #e6e9ef; --muted: #9aa4b5; --line: #2c3440; --agent: #1f3a5c;
  --client: #262d38; --other: #3a3122; --warn: #fdba74; --warn-bg: #3b2414;
  --tag: #262f3b; } }
* { box-sizing: border-box; }
body { margin: 0; padding: 16px; background: var(--bg); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 960px; margin: 0 auto; }
section { background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 12px 16px; margin: 0 0 12px; overflow-wrap: anywhere; }
h1 { font-size: 20px; margin: 0 0 12px; }
h2 { font-size: 16px; margin: 0 0 8px; }
dl { margin: 0; display: grid; grid-template-columns: minmax(8em, max-content) 1fr;
  gap: 2px 12px; }
dt { color: var(--muted); }
dd { margin: 0; min-width: 0; }
ul { margin: 0; padding-left: 20px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: 4px 6px; border-top: 1px solid var(--line);
  vertical-align: top; }
.state { display: inline-block; padding: 0 8px; border-radius: 99px;
  background: var(--tag); font-size: 13px; }
.warn { background: var(--warn-bg); color: var(--warn); font-weight: 600;
  padding: 6px 10px; border-radius: 6px; margin: 0 0 8px; }
.null { color: var(--muted); font-style: italic; }
.chat { display: flex; flex-direction: column; gap: 6px; }
.bubble { max-width: 80%; padding: 6px 10px; border-radius: 12px;
  background: var(--other); }
.bubble.agent { align-self: flex-end; background: var(--agent); }
.bubble.client { align-self: flex-start; background: var(--client); }
.meta { color: var(--muted); font-size: 12px; }
pre { overflow-x: auto; font-size: 12px; margin: 0; }
"""


def esc(value: object) -> str:
    """Any value as escaped text; the payload is model output and a call."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return html.escape(text)


def tree(value: Any) -> str:
    """A JSON value as nested HTML lists, every leaf escaped."""
    if value is None:
        return '<span class="null">null</span>'
    if isinstance(value, dict):
        if not value:
            return '<span class="null">{}</span>'
        rows = "".join(f"<dt>{esc(k)}</dt><dd>{tree(v)}</dd>" for k, v in value.items())
        return f"<dl>{rows}</dl>"
    if isinstance(value, list):
        if not value:
            return '<span class="null">[]</span>'
        return "<ul>" + "".join(f"<li>{tree(v)}</li>" for v in value) + "</ul>"
    return f'<span dir="auto">{esc(value)}</span>'


def card(title: str, body: str, warn: str | None = None) -> str:
    top = "" if warn is None else f'<p class="warn">{esc(warn)}</p>'
    return f"<section><h2>{esc(title)}</h2>{top}{body}</section>"


def details_table(details: Any) -> str:
    """Each detail on one row: its value, its state, and the quote behind it."""
    if not isinstance(details, dict) or not details:
        return tree(details)
    rows = []
    for name, detail in details.items():
        got = detail if isinstance(detail, dict) else {"value": detail}
        rows.append(
            f"<tr><td>{esc(name)}</td><td>{tree(got.get('value'))}</td>"
            f'<td><span class="state">{esc(got.get("state"))}</span></td>'
            f"<td>{tree(got.get('quote'))}</td><td>{esc(got.get('segment'))}</td></tr>"
        )
    head = "<tr><th>detail</th><th>value</th><th>state</th><th>quote</th><th>seg</th>"
    return f"<table>{head}</tr>{''.join(rows)}</table>"


def clock(seconds: object) -> str:
    whole = int(seconds) if isinstance(seconds, int | float) else 0
    return f"{whole // 60:02d}:{whole % 60:02d}"


def bubbles(transcript: Any) -> str:
    """The transcript as chat bubbles: the agent right, the client left."""
    segments = at(transcript, "segments")
    if not isinstance(segments, list) or not segments:
        return '<span class="null">no transcript</span>'
    shown = []
    for n, segment in enumerate(segments, start=1):
        seg = segment if isinstance(segment, dict) else {"text": segment}
        speaker = str(seg.get("speaker"))
        side = speaker if speaker in ("agent", "client") else "other"
        shown.append(
            f'<div class="bubble {side}"><div class="meta">s{n} '
            f"{clock(seg.get('start_s'))} {esc(speaker)}</div>"
            f'<div dir="auto">{esc(seg.get("text"))}</div></div>'
        )
    return f'<div class="chat">{"".join(shown)}</div>'


def report_html(body: dict[str, Any], outcomes: list[dict[str, Any]]) -> str:
    """report.html: one self-contained page of the run, the JSON embedded."""
    result = at(body, "result")
    stage2 = at(body, "stage2_result")
    analysis = at(result, "analysis")
    status = {k: body.get(k) for k in ("job_id", "status", "reason", "delivery")}
    status["stage2"] = body.get("stage2")
    reasons = {
        "analysis_reason": at(result, "analysis_reason"),
        "part_reasons": part_reasons(outcomes),
        "stage2_result.reasons": at(stage2, "reasons"),
        REJECTED_LINE: validation_failures(outcomes) or None,
    }
    summary = {
        "summary": at(analysis, "summary"),
        "crm_note": at(analysis, "crm_note"),
        "mood": at(analysis, "mood"),
        "elements": at(analysis, "elements"),
    }
    pretty = json.dumps(body, ensure_ascii=False, indent=2)
    # Every "<" escaped keeps the JSON from closing its script tag; it parses back.
    embedded = pretty.replace("<", "\\u003c")
    cards = [
        card("Status", tree(status)),
        card("Reasons", tree(reasons)),
        card("Summary", tree(summary)),
        card("Details", details_table(at(analysis, "details"))),
        card("Signals", tree(at(result, "signals"))),
        card("Roles", tree(at(result, "roles"))),
        card("Objections", tree(at(stage2, "objections"))),
        card(
            "Score",
            tree(at(stage2, "score")),
            warn="LOCAL TEST: never for judging a person",
        ),
        card("Escalations", tree(at(stage2, "escalations"))),
        card("Coaching", tree(at(stage2, "coaching"))),
        card("Extras", tree(at(stage2, "extras"))),
        card("Transcript", bubbles(at(result, "transcript"))),
        card(
            "Status JSON",
            f"<details><summary>show</summary><pre>{esc(pretty)}</pre></details>",
        ),
    ]
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>Call report</title><style>{PAGE_STYLE}</style></head><body><main>"
        f"<h1>Call report: job {esc(body.get('job_id'))}</h1>{''.join(cards)}"
        f'<script type="application/json" id="status-json">{embedded}</script>'
        "</main></body></html>"
    )


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
    vocabulary = None if args.vocabulary is None else vocabulary_terms(args.vocabulary)
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
    # The started workers by log name, command-line queue and arq queue, and
    # the health-check values they wrote first: what the stop may delete, and
    # nothing else.
    owned = [
        (name, arg, arq_queue)
        for name, arg, arq_queue in OWN_WORKERS
        if not (args.no_stage2 and arq_queue == STAGE2_QUEUE)
    ]
    claimed: dict[str, bytes] = {}

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
            children.start(name, [python, "-m", "dodeal_ai.workers.calls", arg])
            for name, arg, _ in owned
        ]
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
        claim_health_keys(
            queue, [arq_queue for _, _, arq_queue in owned], CLAIM_SECONDS, claimed
        )

        audio_url = served_audio_url(demo_log, 20)
        if audio_url is None:
            raise fail(f"the demo server printed no audio URL; see {demo_log}")

        section = unit_b_section(
            args, f"http://127.0.0.1:{demo_port}/callback", vocabulary
        )
        if vocabulary is not None:
            print(f"vocabulary: {len(vocabulary)} terms")
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
            began = time.monotonic()
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
            while time.monotonic() - began < args.timeout:
                got = client.get(f"/api/v1/calls/jobs/{job_id}", headers=headers())
                if got.status_code == 200:
                    body = got.json()
                    state = (
                        body.get("status"),
                        body.get("delivery"),
                        body.get("stage2"),
                    )
                    if state != seen:
                        elapsed = round(time.monotonic() - began, 1)
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
            for name in (*OUTCOME_LINES, REJECTED_LINE)
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
        (out / "report.html").write_text(report_html(body, outcomes), encoding="utf-8")
        print(text)
        print(f"\nsaved to {out}")
        if code == EXIT_TIMEOUT:
            print(f"timed out after {args.timeout} s")
        return code
    except KeyboardInterrupt:
        print("interrupted; stopping")
        return EXIT_INTERRUPTED
    finally:
        shut_down(children, queue, claimed, owned)


def shut_down(
    children: Children,
    client: Any,
    claimed: dict[str, bytes],
    owned: list[tuple[str, str, str]],
) -> None:
    """Stop every child, then release our workers' health-check keys; the
    release runs even when stopping the children is itself interrupted."""
    died = set(children.dead())
    try:
        children.stop()
    finally:
        stop_owned(client, claimed, owned, died)


def stop_owned(
    client: Any,
    claimed: dict[str, bytes],
    owned: list[tuple[str, str, str]],
    died: set[str],
) -> None:
    """Release the health-check keys of the workers this run started and
    stopped itself, `owned` as (log name, command-line queue, arq queue);
    never one whose worker exited on its own (its key may not be its own),
    never another worker's. Says which, or why not."""
    ours = {arq_queue for name, _, arq_queue in owned if name not in died}
    try:
        released = release_health_keys(
            client, {q: v for q, v in claimed.items() if q in ours}
        )
    except redis.RedisError:
        print("health-check keys not released: the queue Redis did not answer")
        return
    print(f"health-check keys released: {', '.join(released) or 'none'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("audio", type=Path)
    parser.add_argument("--language", choices=["en", "ar", "mixed"], default="mixed")
    parser.add_argument("--tenant", default="tenant-a")
    parser.add_argument("--stt-profile", default="")
    parser.add_argument("--no-stage2", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=1800.0)
    parser.add_argument("--vocabulary", type=Path, default=None)
    parser.add_argument("--dialect", choices=list(get_args(WhatsAppDialect.__value__)))
    return run(parser.parse_args(argv))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
