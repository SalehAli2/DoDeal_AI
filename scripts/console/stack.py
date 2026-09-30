"""The console's stack, and every call and note it sends (the page is app.py).

ONE STACK FOR THE WHOLE SESSION. start() brings up the API, the normal and
stage-2 call workers and the file-and-callback server (files.py) as child
processes, with call_e2e.py's own pieces: its refusals (production, the fake
speech-to-text, a Redis that does not answer, other workers on the call
queues), its demo environment (the service signing key from .env.demo,
loopback audio allowed, a fresh callback secret) and its health-key claim, so
stop() deletes only the keys its own workers wrote. stop() also runs when the
console exits.

A CALL is saved under the console folder, never in this repository, under a
random name only files.py serves; pushed as the CRM pushes it; followed on the
status route; and, once finished, reported in its own folder exactly as
call_e2e.py reports a run (status.json, outcomes.json, report.txt and
report.html).

A NOTE goes to the direct route with the four lead fields the classifier
reads. At most MAX_NOTES_PER_RUN in one go, refused before any is sent, and
never retried: every judgement is paid for.

NOTHING HERE LOGS a note, a transcript or a result. They are shown on the page
and saved in the console folder, which is refused inside the repository.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import redis

from dodeal_ai.core.config import ConfigError
from scripts import call_e2e as e2e
from scripts.console.files import AUDIO_PREFIX

# Where the console keeps uploads, reports and logs when no folder is given:
# outside every repository, in the user's home.
CONSOLE_DIR_ENV = "DODEAL_CONSOLE_DIR"
DEFAULT_CONSOLE_DIR = Path.home() / "dodeal-console"

# The most notes one click may send: each is a paid judgement of three model
# passes. diagnose_notes.py holds a hand run to the same kind of cap; higher
# lets a wrong file spend a day's budget before anyone looks.
MAX_NOTES_PER_RUN = 20

# The recording types the page takes: what ffprobe reads and phones record.
AUDIO_SUFFIXES = (".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus", ".aac", ".amr")

# The invented lead and author every call and note is sent for unless the page
# says otherwise, as in call_e2e.py and call_demo.py.
DEMO_LEAD_ID = 1004
DEMO_AUTHOR_ID = 7

# The columns a notes file may carry; note_text is the one required.
NOTE_COLUMNS = (
    "note_text",
    "lead_id",
    "author_id",
    "leadType",
    "enquiryType",
    "project",
    "status",
    "deal_type",
    "stage_change_to",
)
_LEAD_FIELDS = ("leadType", "enquiryType", "project", "status", "deal_type")
_DIRECT_ROUTE = "/api/v1/notes/judgements/direct"
_STEM = re.compile(r"[^A-Za-z0-9_-]+")


class ConsoleError(Exception):
    """A refusal or a failure, its text safe to show: fixed words, codes and
    counts, never a note, a transcript or a model answer."""


def console_dir(raw: str | None = None) -> Path:
    """The console folder, made if missing; refused inside this repository."""
    chosen = Path(raw).expanduser() if raw else DEFAULT_CONSOLE_DIR
    resolved = chosen.resolve()
    if resolved.is_relative_to(e2e.REPO_ROOT):
        raise ConsoleError(
            "the console folder must be outside the repository: uploads are "
            "real calls and notes"
        )
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def stamp() -> str:
    """Now, as a folder name's prefix."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def safe_stem(name: str) -> str:
    """A file name's stem as a folder name: letters, digits, - and _ only."""
    stem = _STEM.sub("_", Path(name).stem).strip("_")
    return (stem or "call")[:40]


def audio_suffix(name: str) -> str:
    """The upload's suffix, lower case; refused when not a recording type."""
    suffix = Path(name).suffix.lower()
    if suffix not in AUDIO_SUFFIXES:
        raise ConsoleError(f"not a recording type: use one of {AUDIO_SUFFIXES}")
    return suffix


@dataclass(frozen=True, slots=True)
class Options:
    """What the page sets for a run: the tenant, and the unit_b settings
    call_e2e.py's flags set (--stt-profile, --channels, --dialect,
    --vocabulary, --no-stage2)."""

    tenant: str = "tenant-a"
    stt_profile: str = ""
    channels: str | None = None
    dialect: str | None = None
    vocabulary: tuple[str, ...] | None = None
    stage2: bool = True


@dataclass(slots=True)
class CallRun:
    """One uploaded recording: where it is kept, its job, and the job as last
    read."""

    name: str
    folder: Path
    audio: Path
    seconds: float
    language: str
    job_id: str | None = None
    body: dict[str, Any] = field(default_factory=dict)
    reported: bool = False

    @property
    def state(self) -> str:
        """status / delivery / stage2, as the status route last said."""
        if not self.body:
            return "pushed"
        parts = (self.body.get(k) for k in ("status", "delivery", "stage2"))
        return " / ".join(str(part) for part in parts)


@dataclass(frozen=True, slots=True)
class NoteInput:
    """One note to judge, and the lead fields the classifier reads."""

    note_text: str
    lead_id: int = DEMO_LEAD_ID
    author_id: int = DEMO_AUTHOR_ID
    leadType: str | None = None
    enquiryType: str | None = None
    project: str | None = None
    status: str | None = None
    deal_type: str | None = None
    stage_change_to: str | None = None

    def body(self, note_id: int) -> dict[str, Any]:
        """The direct route's body (DirectJudgementRequest)."""
        lead = {name: getattr(self, name) for name in _LEAD_FIELDS}
        body: dict[str, Any] = {
            "lead_id": self.lead_id,
            "note_id": note_id,
            "author_id": self.author_id,
            "note_text": self.note_text,
            "lead": {k: v for k, v in lead.items() if v is not None},
        }
        if self.stage_change_to is not None:
            body["stage_change_to"] = self.stage_change_to
        return body


@dataclass(frozen=True, slots=True)
class NoteRun:
    """One note sent: its id, what came back, and where it is kept."""

    note_id: int
    code: int
    answer: dict[str, Any]
    saved: Path


def _blank_to_none(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


def _id(value: str | None, default: int, row: int, column: str) -> int:
    """A positive whole id from a cell, or the default for a blank one."""
    text = (value or "").strip()
    if not text:
        return default
    if not text.isdigit() or int(text) < 1:
        raise ConsoleError(f"row {row}: {column} must be a whole number above 0")
    return int(text)


def notes_from_csv(text: str) -> list[NoteInput]:
    """The notes of a CSV file with a header row: note_text required, the other
    NOTE_COLUMNS optional, any other column refused, rows with every cell blank
    skipped. A refusal names the line and the column, never a cell's text."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = [name.strip() for name in reader.fieldnames or []]
    unknown = [name for name in columns if name not in NOTE_COLUMNS]
    if "note_text" not in columns:
        raise ConsoleError("the notes file needs a note_text column")
    if unknown:
        raise ConsoleError(f"unknown columns: {', '.join(unknown)}")
    notes = []
    for raw in reader:
        row = reader.line_num
        if None in raw:
            raise ConsoleError(f"row {row}: more cells than the header has columns")
        cells = {str(k).strip(): v for k, v in raw.items()}
        if not any((value or "").strip() for value in cells.values()):
            continue
        note = (cells.get("note_text") or "").strip()
        if not note:
            raise ConsoleError(f"row {row}: note_text is empty")
        notes.append(
            NoteInput(
                note_text=note,
                lead_id=_id(cells.get("lead_id"), DEMO_LEAD_ID, row, "lead_id"),
                author_id=_id(cells.get("author_id"), DEMO_AUTHOR_ID, row, "author_id"),
                **{
                    name: _blank_to_none(cells.get(name))
                    for name in (*_LEAD_FIELDS, "stage_change_to")
                },
            )
        )
    if not notes:
        raise ConsoleError("the notes file has no rows")
    return notes


def reason_of(response: httpx.Response) -> str:
    """The service's refusal as a fixed code: its body's `reason`, else the
    status line. The service's error bodies never quote input back."""
    try:
        body = response.json()
    except ValueError:
        body = None
    reason = body.get("reason") if isinstance(body, dict) else None
    return f"{response.status_code} {reason or response.reason_phrase}"


def past_reports(root: Path) -> list[Path]:
    """Every call folder under the console folder with a report, newest first."""
    calls = root / "calls"
    if not calls.is_dir():
        return []
    found = [path for path in calls.iterdir() if (path / "report.html").is_file()]
    return sorted(found, key=lambda path: path.name, reverse=True)


class Stack:
    """The API, the call workers and files.py, started once; the calls and
    notes sent through them. Thread-safe: every page session shares one."""

    def __init__(self, root: Path, *, python: str = sys.executable) -> None:
        self.root = root
        self.python = python
        self.options = Options()
        self.calls: list[CallRun] = []
        self.notes: list[NoteRun] = []
        self.logs: Path | None = None
        self._lock = threading.RLock()
        self._children: e2e.Children | None = None
        self._queue: Any = None
        self._claimed: dict[str, bytes] = {}
        self._owned: list[tuple[str, str, str]] = []
        self._worker_logs: list[Path] = []
        self._files_log: Path | None = None
        self._files_port = 0
        self._api = ""
        self._key = ""
        self._issuer = ""
        self._audience = ""
        self._host = ""
        self._tenant = ""
        self._ids = 0

    # --- the stack ---------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._children is not None

    def dead(self) -> list[str]:
        """The children that exited on their own."""
        return [] if self._children is None else self._children.dead()

    def start(self, options: Options) -> None:
        """Bring the stack up with `options`, or raise ConsoleError with why;
        a start that fails stops whatever it had started."""
        with self._lock:
            if self.running:
                raise ConsoleError("the stack is already running")
            try:
                self._start(options)
            except SystemExit as refused:
                self._shut()
                raise ConsoleError(str(refused).removeprefix("call_e2e: ")) from None
            except ConfigError:
                self._shut()
                raise ConsoleError(
                    "the service settings did not load: check .env against .env.example"
                ) from None
            except (httpx.HTTPError, redis.RedisError, OSError) as failed:
                self._shut()
                raise ConsoleError(
                    f"the stack did not start: {type(failed).__name__}"
                ) from None
            except ConsoleError:
                self._shut()
                raise

    def _start(self, options: Options) -> None:
        settings = e2e.get_settings()
        environment = os.environ.get("DODEAL_ENVIRONMENT") or e2e.plain(
            getattr(settings, "environment", "development")
        )
        if environment.lower() == "production":
            raise ConsoleError("refused: DODEAL_ENVIRONMENT is production")
        provider = e2e.plain(getattr(settings, "call_stt_provider", "fake"))
        if provider == "fake" and not options.stt_profile:
            raise ConsoleError(
                "the fake speech-to-text is refused: set DODEAL_CALL_STT_PROVIDER "
                "or give a speech-to-text profile"
            )
        try:
            operational = e2e.plain(settings.redis_operational_url)
            redis.Redis.from_url(operational, socket_connect_timeout=2).ping()
        except (redis.RedisError, AttributeError, ValueError):
            raise ConsoleError(
                "Redis did not answer. Run: docker compose up -d redis"
            ) from None
        queue = redis.Redis.from_url(e2e.store_url("queue"), socket_connect_timeout=2)
        e2e.refuse_other_workers(queue)
        self._queue = queue

        key = e2e.env_file_values(e2e.DEMO_ENV).get("DODEAL_SERVICE_JWT_SIGNING_KEY")
        if not key:
            raise ConsoleError("no DODEAL_SERVICE_JWT_SIGNING_KEY in .env.demo")
        self._key = key
        self._issuer = e2e.plain(getattr(settings, "service_jwt_issuer", "dodeal-crm"))
        self._audience = e2e.plain(
            getattr(settings, "service_jwt_audience", "dodeal-ai")
        )
        domain = e2e.plain(getattr(settings, "inbound_base_domain", "dodealcrm.com"))
        self._tenant = options.tenant
        self._host = f"{options.tenant}.{domain}"
        secret = secrets.token_hex(16)

        env = {**e2e.env_file_values(e2e.DOT_ENV), **os.environ}
        env.update(
            {
                "DODEAL_TENANT_CONFIG_CACHE_SECONDS": "1",
                "DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO": "true",
                "DODEAL_CALL_CALLBACK_SECRETS": json.dumps({options.tenant: secret}),
                "DODEAL_SERVICE_JWT_ALGORITHM": "HS256",
                "DODEAL_SERVICE_JWT_SIGNING_KEY": key,
                "PYTHONUNBUFFERED": "1",
            }
        )
        self.logs = self.root / "logs" / stamp()
        self.logs.mkdir(parents=True, exist_ok=True)
        (self.root / "audio").mkdir(exist_ok=True)
        children = e2e.Children(self.logs, env)
        self._children = children

        api_port, self._files_port = e2e.free_port(), e2e.free_port()
        self._api = f"http://127.0.0.1:{api_port}"
        children.start(
            "api",
            [
                self.python,
                "-m",
                "uvicorn",
                "dodeal_ai.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
        )
        self._owned = [
            (name, arg, arq_queue)
            for name, arg, arq_queue in e2e.OWN_WORKERS
            if options.stage2 or arq_queue != e2e.STAGE2_QUEUE
        ]
        self._worker_logs = [
            children.start(name, [self.python, "-m", "dodeal_ai.workers.calls", arg])
            for name, arg, _ in self._owned
        ]
        self._files_log = children.start(
            "files",
            [
                self.python,
                "-m",
                "scripts.console.files",
                "--port",
                str(self._files_port),
                "--secret",
                secret,
                "--dir",
                str(self.root / "audio"),
            ],
        )
        if e2e.wait_for_http(f"{self._api}/health", 60) is None:
            raise ConsoleError(f"the API did not start; see {self.logs / 'api.log'}")
        time.sleep(3)
        if children.dead():
            raise ConsoleError(
                f"exited at start: {', '.join(children.dead())}; see {self.logs}"
            )
        e2e.claim_health_keys(
            queue,
            [arq_queue for _, _, arq_queue in self._owned],
            e2e.CLAIM_SECONDS,
            self._claimed,
        )
        self._put_settings(options)

    def apply(self, options: Options) -> None:
        """Send new unit_b settings to the running stack; the tenant and
        stage 2 need a restart, since they shape what was started."""
        with self._lock:
            if not self.running:
                raise ConsoleError("start the stack first")
            if (options.tenant, options.stage2) != (
                self.options.tenant,
                self.options.stage2,
            ):
                raise ConsoleError("the tenant and stage 2 change only on a restart")
            self._put_settings(options)

    def _put_settings(self, options: Options) -> None:
        flags = SimpleNamespace(
            stt_profile=options.stt_profile,
            dialect=options.dialect,
            channels=options.channels,
        )
        vocabulary = None if options.vocabulary is None else list(options.vocabulary)
        section = e2e.unit_b_section(
            flags,  # type: ignore[arg-type]
            f"http://127.0.0.1:{self._files_port}/callback",
            vocabulary,
        )
        with self._client() as client:
            put = client.put(
                "/api/v1/admin/tenant-config/unit_b",
                json=section,
                headers=self._headers(),
            )
        if put.status_code >= 300:
            raise ConsoleError(f"the unit_b settings were refused: {reason_of(put)}")
        self.options = options

    def stop(self) -> None:
        """Stop every child and release the health keys our workers wrote.
        Calls and notes sent stay listed, and their folders stay on disk."""
        with self._lock:
            self._shut()

    def _shut(self) -> None:
        children, self._children = self._children, None
        if children is None:
            return
        if self._queue is None:
            children.stop()
            return
        e2e.shut_down(children, self._queue, self._claimed, self._owned)
        self._claimed = {}

    # --- sending -------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        """The CRM's service token for the running tenant, as call_e2e.py
        mints it: signed with .env.demo's key, four minutes long."""
        now = int(time.time())
        claims = {
            "iss": self._issuer,
            "aud": self._audience,
            "subdomain": self._tenant,
            "iat": now,
            "exp": now + e2e.TOKEN_LIFETIME_SECONDS,
        }
        token = jwt.encode(claims, self._key, algorithm="HS256")
        return {"Host": self._host, "Authorization": f"Bearer {token}"}

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self._api, timeout=60)

    def _next_id(self) -> int:
        """An id no earlier send of this console used: the clock in
        milliseconds, kept under a billion and never repeated."""
        with self._lock:
            self._ids = max(self._ids + 1, int(time.time() * 1000) % 1_000_000_000)
            return max(1, self._ids)

    def push_call(
        self,
        name: str,
        data: bytes,
        language: str,
        *,
        seconds_of: Callable[[Path], float] = e2e.audio_seconds,
    ) -> CallRun:
        """Save one uploaded recording and push it as the CRM would, then list
        it; a refused push raises ConsoleError with the service's reason."""
        if not self.running:
            raise ConsoleError("start the stack first")
        suffix = audio_suffix(name)
        audio = self.root / "audio" / f"{secrets.token_hex(16)}{suffix}"
        audio.write_bytes(data)
        folder = self.root / "calls" / f"{stamp()}_{safe_stem(name)}"
        folder.mkdir(parents=True, exist_ok=True)
        try:
            seconds = seconds_of(audio)
        except SystemExit:
            raise ConsoleError("ffprobe could not read the recording") from None
        call = CallRun(name, folder, audio, seconds, language)
        now = datetime.now(UTC)
        push = {
            "call_id": self._next_id(),
            "lead_id": DEMO_LEAD_ID,
            "author_id": DEMO_AUTHOR_ID,
            "duration_seconds": max(1, round(seconds)),
            "recorded_at": now.isoformat(timespec="seconds"),
            "audio_url": (
                f"http://127.0.0.1:{self._files_port}{AUDIO_PREFIX}{audio.name}"
            ),
            "audio_url_expires_at": (now + timedelta(hours=1)).isoformat(
                timespec="seconds"
            ),
            "language_hint": language,
        }
        with self._client() as client:
            pushed = client.post(
                "/api/v1/calls/jobs", json=push, headers=self._headers()
            )
        if pushed.status_code != 202:
            raise ConsoleError(f"the push was refused: {reason_of(pushed)}")
        call.job_id = str(pushed.json()["job_id"])
        with self._lock:
            self.calls.append(call)
        return call

    def refresh(self, call: CallRun) -> CallRun:
        """Read the job again; once it is finished, write its reports once."""
        if call.job_id is None or call.reported or not self.running:
            return call
        with self._client() as client:
            got = client.get(
                f"/api/v1/calls/jobs/{call.job_id}", headers=self._headers()
            )
        if got.status_code != 200:
            return call
        call.body = got.json()
        if e2e.finished(call.body, self.options.stage2):
            self.write_reports(call)
        return call

    def outcomes(self, job_id: str) -> list[dict[str, Any]]:
        """The workers' outcome, refused-answer and quote_miss lines for one
        job: ids, counts and codes only, as they were logged."""
        names = (*e2e.OUTCOME_LINES, e2e.REJECTED_LINE, e2e.MISS_LINE)
        return [
            record
            for log in self._worker_logs
            for name in names
            for record in e2e.outcome_records(log, name)
            if record.get("job_id") == job_id
        ]

    def callbacks(self, job_id: str) -> list[str]:
        """The callbacks files.py took for one job: events and headers only."""
        if self._files_log is None or not self._files_log.exists():
            return []
        text = self._files_log.read_text(encoding="utf-8", errors="replace")
        return e2e.callbacks_for(text, self.options.tenant, job_id)

    def write_reports(self, call: CallRun) -> None:
        """The job's status, outcome lines and report, text and page, in its
        folder: what call_e2e.py writes for a run, never printed."""
        assert call.job_id is not None
        outcomes = self.outcomes(call.job_id)
        text = "\n".join(
            [
                *e2e.report(call.body, outcomes),
                "",
                "=== CALLBACKS ===",
                *self.callbacks(call.job_id),
            ]
        )
        files = {
            "status.json": json.dumps(call.body, ensure_ascii=False, indent=2),
            "outcomes.json": json.dumps(outcomes, ensure_ascii=False, indent=2),
            "report.txt": text,
            "report.html": e2e.report_html(call.body, outcomes),
        }
        for name, content in files.items():
            (call.folder / name).write_text(content, encoding="utf-8")
        call.reported = True

    def judge(self, note: NoteInput, *, resubmission: bool = False) -> NoteRun:
        """Send one note to the direct route and keep the answer, whatever it
        is, beside the note in the console folder."""
        if not self.running:
            raise ConsoleError("start the stack first")
        note_id = self._next_id()
        route = _DIRECT_ROUTE + ("/resubmission" if resubmission else "")
        with self._client() as client:
            sent = client.post(route, json=note.body(note_id), headers=self._headers())
        try:
            answer = sent.json()
        except ValueError:
            answer = {}
        if not isinstance(answer, dict):
            answer = {}
        if sent.status_code != 200:
            answer = {"refused": reason_of(sent)}
        folder = self.root / "notes"
        folder.mkdir(exist_ok=True)
        saved = folder / f"{stamp()}_{note_id}.json"
        saved.write_text(
            json.dumps(
                {
                    "sent": note.body(note_id),
                    "code": sent.status_code,
                    "answer": answer,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        run = NoteRun(note_id, sent.status_code, answer, saved)
        with self._lock:
            self.notes.append(run)
        return run

    def judge_many(
        self, notes: Iterable[NoteInput], *, resubmission: bool = False
    ) -> list[NoteRun]:
        """Each note in turn, one answer each and never a retry; more than
        MAX_NOTES_PER_RUN is refused before any is sent."""
        batch = list(notes)
        if len(batch) > MAX_NOTES_PER_RUN:
            raise ConsoleError(
                f"{len(batch)} notes: at most {MAX_NOTES_PER_RUN} in one go"
            )
        return [self.judge(note, resubmission=resubmission) for note in batch]


def note_summary(run: NoteRun) -> dict[str, object]:
    """One judgement as a table row: the band and total or why it was
    suppressed, the note type, the action and the question asked, if any."""
    answer = run.answer
    analysis = answer.get("analysis") or {}
    score = answer.get("score") or {}
    decision = answer.get("decision") or {}
    suppressed = answer.get("suppressed") or {}
    return {
        "note": run.note_id,
        "http": run.code,
        "band": score.get("band"),
        "total": score.get("total"),
        "type": analysis.get("note_type"),
        "vague": analysis.get("is_vague"),
        "action": decision.get("action"),
        "suppressed": suppressed.get("reason"),
        "question": analysis.get("clarification_prompt")
        or suppressed.get("clarification_prompt"),
        "refused": answer.get("refused"),
    }


def call_summary(call: CallRun) -> dict[str, object]:
    """One call as a table row: the file, its length, its job and where the
    job is, and the score's band once stage 2 is done (a local test only)."""
    score = (call.body.get("stage2_result") or {}).get("score") or {}
    return {
        "file": call.name,
        "seconds": round(call.seconds),
        "job": call.job_id,
        "status / delivery / stage 2": call.state,
        "band (local test)": score.get("band"),
        "report": "ready" if call.reported else "",
    }
