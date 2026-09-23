"""Call-job observability (register item 54): every worker line names its
tenant, job and request, each run ends in ONE complete outcome line with no
content, and the call metrics move with no tenant label."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from dodeal_ai.core import metrics
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import create_job
from dodeal_ai.core.logging_config import (
    JobContextFilter,
    JsonFormatter,
    configure_logging,
    job_log_context,
)
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.delivery import deliver_callback
from dodeal_ai.units.call_intelligence.fake_transcriber import (
    DEFAULT_SEGMENTS,
    FakeTranscriber,
)
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, enqueue_call
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.worker import process_call

HOST = "audio.tenant-a.example"
LINK = f"https://{HOST}/7.wav?sig=SIGNED-LINK"
PHONE = "cd" * 32
JOB = "job-1"
OUTCOME_FIELDS = {
    "status",
    "reason",
    "attempts",
    "queue",
    "duration_seconds",
    "bytes",
    "language_profile",
    "uncertain",
    "download_ms",
    "transcribe_ms",
    "analyse_ms",
    "deliver_ms",
    "elapsed_ms",
    "provider",
    "model",
    "pass_tokens",
}


def _source(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=b"RIFF" + b"\x00" * 90, headers={"content-type": "audio/wav"}
    )


async def _resolve(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_source)) as http:
        yield {"http": http, "transcriber": FakeTranscriber(), "resolve": _resolve}


@pytest.fixture
def lines() -> Iterator[io.StringIO]:
    """The JSON stream with the job filter on its handler, as configured."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(JobContextFilter())
    root = logging.getLogger()
    original = logging.getLogger("dodeal_ai").level
    root.addHandler(handler)
    logging.getLogger("dodeal_ai").setLevel(logging.INFO)
    yield stream
    root.removeHandler(handler)
    logging.getLogger("dodeal_ai").setLevel(original)


def _parsed(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _sample(name: str, **labels: str) -> float:
    return metrics.REGISTRY.get_sample_value(name, labels) or 0.0


async def _push(**changes: object) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {"calls_enabled": True, "audio_hosts": [HOST]},
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )
    body = CallJobRequest.model_validate(
        {
            "call_id": 7,
            "lead_id": 1656,
            "author_id": 27,
            "duration_seconds": 150,
            "recorded_at": "2026-09-23T08:00:00+00:00",
            "audio_url": LINK,
            "audio_url_expires_at": (
                datetime.now(UTC) + timedelta(hours=1)
            ).isoformat(),
            "lead_phone_hash": PHONE,
            **changes,
        }
    )
    await create_job(
        "tenant-a",
        7,
        job_id=JOB,
        request_id="req-push",
        queue=NORMAL_QUEUE,
        metadata=job_metadata(body, parse_unit_b_section({})),
        now=datetime.now(UTC),
    )


# --- the guard ------------------------------------------------------------------


async def test_the_outcome_line_is_complete_and_carries_no_content(
    ctx: dict, lines: io.StringIO
) -> None:
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    parsed = _parsed(lines)
    (outcome,) = [line for line in parsed if line["message"] == "call_job_outcome"]
    assert OUTCOME_FIELDS <= set(outcome)
    assert (outcome["status"], outcome["reason"], outcome["attempts"]) == (
        "done",
        None,
        1,
    )
    assert (outcome["queue"], outcome["duration_seconds"], outcome["bytes"]) == (
        NORMAL_QUEUE,
        150,
        94,
    )
    assert (outcome["language_profile"], outcome["uncertain"]) == ("mixed", False)
    assert (outcome["provider"], outcome["model"]) == ("fake", "fake-stt-1")
    assert all(
        isinstance(outcome[k], int)
        for k in ("download_ms", "transcribe_ms", "deliver_ms")
    )
    assert isinstance(outcome["analyse_ms"], int)
    assert (outcome["pass_tokens"], outcome["analysis_reason"]) == (
        None,
        "llm_not_configured",
    )
    text = lines.getvalue()
    for secret in ["SIGNED-LINK", HOST, PHONE, *(s.text for s in DEFAULT_SEGMENTS)]:
        assert secret not in text


async def test_every_line_of_a_run_names_tenant_job_and_request(
    ctx: dict, lines: io.StringIO
) -> None:
    await _push()
    lines.truncate(0)
    lines.seek(0)
    await process_call(ctx, "tenant-a", JOB)
    parsed = [line for line in _parsed(lines) if line["logger"].startswith("dodeal_ai")]
    assert {"audio_seconds_charged", "call_job_outcome"} <= {
        x["message"] for x in parsed
    }
    for line in parsed:
        assert (line["tenant"], line["job_id"], line["request_id"]) == (
            "tenant-a",
            JOB,
            "req-push",
        ), line["message"]


async def test_a_free_outcome_has_no_stage_but_delivery(
    ctx: dict, lines: io.StringIO
) -> None:
    await _push(call_outcome="voicemail")
    await process_call(ctx, "tenant-a", JOB)
    (outcome,) = [x for x in _parsed(lines) if x["message"] == "call_job_outcome"]
    assert (outcome["status"], outcome["reason"]) == ("done", "voicemail")
    assert (outcome["download_ms"], outcome["transcribe_ms"], outcome["bytes"]) == (
        None,
        None,
        None,
    )
    assert (outcome["language_profile"], outcome["provider"]) == (None, None)
    assert isinstance(outcome["deliver_ms"], int)


async def test_a_failure_is_a_warning_and_a_crash_is_an_error_status(
    ctx: dict, lines: io.StringIO
) -> None:
    await _push(audio_url_expires_at="2026-01-01T00:00:00+00:00")
    await process_call(ctx, "tenant-a", JOB)
    (failed,) = [x for x in _parsed(lines) if x["message"] == "call_job_outcome"]
    assert (failed["level"], failed["status"], failed["reason"]) == (
        "WARNING",
        "failed",
        "audio_link_expired",
    )


async def test_a_crash_ends_the_run_with_status_error(
    ctx: dict, lines: io.StringIO
) -> None:
    await _push(call_outcome="voicemail")
    before = _sample("call_jobs_total", status="error")

    async def crash(job, event, config) -> bool:
        raise RuntimeError("the worker died")

    ctx["deliver"] = crash
    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    (outcome,) = [x for x in _parsed(lines) if x["message"] == "call_job_outcome"]
    assert (outcome["level"], outcome["status"]) == ("WARNING", "error")
    assert _sample("call_jobs_total", status="error") == before + 1


async def test_a_missing_job_writes_no_outcome_line(
    ctx: dict, lines: io.StringIO
) -> None:
    await process_call(ctx, "tenant-a", "nope")
    assert not [x for x in _parsed(lines) if x["message"] == "call_job_outcome"]


# --- the metrics ------------------------------------------------------------------


async def test_the_call_metrics_move(ctx: dict) -> None:
    before = {
        "done": _sample("call_jobs_total", status="done"),
        "runs": _sample("call_job_seconds_count"),
        "download": _sample("call_stage_seconds_count", stage="download"),
        "transcribe": _sample("call_stage_seconds_count", stage="transcribe"),
        "deliver": _sample("call_stage_seconds_count", stage="deliver"),
        "audio": _sample("audio_seconds_processed_total"),
    }
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    assert _sample("call_jobs_total", status="done") == before["done"] + 1
    assert _sample("call_job_seconds_count") == before["runs"] + 1
    for stage in ("download", "transcribe", "deliver"):
        assert _sample("call_stage_seconds_count", stage=stage) == before[stage] + 1
    assert _sample("audio_seconds_processed_total") == before["audio"] + 150


async def test_callback_attempts_are_counted_and_named_by_job(
    ctx: dict, lines: io.StringIO
) -> None:
    await _push()
    before = _sample(
        "callback_deliveries_total", event="call.stage1", outcome="no_callback"
    )
    from dodeal_ai.core import jobs as jobs_module
    from dodeal_ai.core.jobs import job_key

    await jobs_module.get_jobs_client().hset(
        job_key("tenant-a", JOB), mapping={"status": "done", "delivery": "pending"}
    )
    await deliver_callback(ctx, "tenant-a", JOB, "call.stage1", 1)
    after = _sample(
        "callback_deliveries_total", event="call.stage1", outcome="no_callback"
    )
    assert after == before + 1
    (line,) = [x for x in _parsed(lines) if x["message"] == "callback_attempt"]
    assert (line["job_id"], line["request_id"]) == (JOB, "req-push")


async def test_the_queue_depth_is_read_when_the_page_is_served(monkeypatch) -> None:
    monkeypatch.setenv("DODEAL_METRICS_ENABLED", "true")
    get_settings.cache_clear()
    await enqueue_call("tenant-a", "job-a", NORMAL_QUEUE)
    await enqueue_call("tenant-a", "job-b", NORMAL_QUEUE)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        page = (await c.get("/metrics")).text
    assert 'call_queue_depth{queue="arq:calls:normal"} 2.0' in page
    assert 'call_queue_depth{queue="arq:calls:priority"} 0.0' in page


async def test_a_queue_that_cannot_be_read_keeps_its_last_depth(monkeypatch) -> None:
    import fakeredis
    from arq.connections import ArqRedis

    from dodeal_ai.units.call_intelligence import queues

    metrics.CALL_QUEUE_DEPTH.labels(queue=NORMAL_QUEUE).set(7)
    server = fakeredis.FakeServer()
    server.connected = False
    dead = ArqRedis(
        connection_pool=fakeredis.FakeAsyncRedis(server=server).connection_pool
    )
    monkeypatch.setattr(queues, "get_queue_client", lambda: dead)
    await queues.refresh_queue_depths()
    assert _sample("call_queue_depth", queue=NORMAL_QUEUE) == 7


def test_no_call_metric_carries_an_identity_label() -> None:
    labels = {
        name
        for metric in (
            metrics.CALL_JOBS,
            metrics.CALL_JOB_SECONDS,
            metrics.CALL_STAGE_SECONDS,
            metrics.AUDIO_SECONDS_PROCESSED,
            metrics.CALLBACK_DELIVERIES,
            metrics.CALL_QUEUE_DEPTH,
            metrics.CALL_TOKENS,
        )
        for name in metric._labelnames
    }
    assert labels == {"status", "stage", "event", "outcome", "queue", "pass", "kind"}


# --- the filter ------------------------------------------------------------------


def test_the_filter_adds_nothing_outside_a_job_and_never_overwrites() -> None:
    job_filter = JobContextFilter()
    record = logging.LogRecord("dodeal_ai.x", logging.INFO, "", 0, "m", (), None)
    assert job_filter.filter(record) and not hasattr(record, "job_id")
    with job_log_context(tenant="tenant-a", job_id="j") as fields:
        fields["request_id"] = "r"
        record.tenant = "given"
        assert job_filter.filter(record)
    assert (record.tenant, record.job_id, record.request_id) == ("given", "j", "r")


def test_configure_logging_puts_the_filter_on_the_handler() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger("dodeal_ai").level)
    try:
        configure_logging()
        (handler,) = root.handlers
        assert any(isinstance(f, JobContextFilter) for f in handler.filters)
    finally:
        root.handlers, root.level = saved[0], saved[1]
        logging.getLogger("dodeal_ai").setLevel(saved[2])


# --- spend on the outcome line (register item "cost") ---------------------------


async def test_the_outcome_line_carries_the_calls_spend(
    ctx: dict, lines: io.StringIO, monkeypatch
) -> None:
    monkeypatch.setenv("DODEAL_STT_PRICES", json.dumps({"fake-stt-1": 0.006}))
    monkeypatch.setenv("DODEAL_PRICE_TABLE_VERSION", "prices-2026-09")
    get_settings.cache_clear()
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    (outcome,) = [x for x in _parsed(lines) if x["message"] == "call_job_outcome"]
    assert (
        outcome["model_calls"],
        outcome["input_tokens"],
        outcome["audio_seconds"],
    ) == (
        0,
        0,
        150,
    )
    assert outcome["cost_usd"] == pytest.approx(150 / 60 * 0.006)
    assert outcome["price_table_version"] == "prices-2026-09"


async def test_a_failed_transcription_logs_its_seconds_unpriced(
    ctx: dict, lines: io.StringIO, monkeypatch
) -> None:
    from dodeal_ai.units.call_intelligence.transcriber import TranscriptionError

    monkeypatch.setenv("DODEAL_STT_PRICES", json.dumps({"fake-stt-1": 0.006}))
    get_settings.cache_clear()
    ctx["transcriber"] = FakeTranscriber(
        fail=TranscriptionError("stt_unavailable", retryable=False)
    )
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    (outcome,) = [x for x in _parsed(lines) if x["message"] == "call_job_outcome"]
    assert (outcome["status"], outcome["audio_seconds"], outcome["cost_usd"]) == (
        "failed",
        150,
        None,
    )
    (warning,) = [x for x in _parsed(lines) if x["message"] == "price_unknown"]
    assert warning["model"] == "unknown"
