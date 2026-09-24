"""GET /api/v1/calls/jobs/{job_id} (register item 50): status, reason and the
result while held, another tenant's job is 404, the reads counter moves, and
every read is one audit line with no content."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator, Iterator

import fakeredis
import httpx
import pytest

from dodeal_ai.core import jobs
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import job_key, store_result, store_stage2_result
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from tests.conftest import RedisFakes
from tests.helpers import tokens

PUSH_URL = "/api/v1/calls/jobs"
CALLS_CONFIG_URL = "/api/v1/admin/tenant-config/unit_b"
TRANSCRIPT_WORDS = "the invented words of an invented call"
LINK = "https://audio.tenant-a.example/7.wav?sig=SIGNED-LINK"


def _headers(token: str | None = None, host: str = "tenant-a") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token or tokens.mint_service_token(subdomain=host)}",
        "Host": f"{host}.dodealcrm.com",
    }


@pytest.fixture
async def client(monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


@pytest.fixture
def audit_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield stream
    logger.removeHandler(handler)
    logger.setLevel(original)


def _reads(stream: io.StringIO) -> list[dict]:
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    return [line for line in lines if line["message"] == "call_job_read"]


async def _pushed(client: httpx.AsyncClient) -> str:
    await client.put(
        CALLS_CONFIG_URL,
        json={"calls_enabled": True, "audio_hosts": ["audio.tenant-a.example"]},
        headers=_headers(),
    )
    response = await client.post(
        PUSH_URL,
        json={
            "call_id": 7,
            "lead_id": 1656,
            "author_id": 27,
            "duration_seconds": 95,
            "recorded_at": "2026-09-23T08:00:00+04:00",
            "audio_url": LINK,
            "audio_url_expires_at": "2026-09-23T10:00:00+04:00",
        },
        headers=_headers(),
    )
    assert response.status_code == 202
    return response.json()["job_id"]


async def test_a_queued_job_reads_back_with_no_result(client) -> None:
    job_id = await _pushed(client)
    response = await client.get(f"{PUSH_URL}/{job_id}", headers=_headers())
    assert response.status_code == 200
    assert response.json() == {
        "job_id": job_id,
        "status": "queued",
        "reason": None,
        "delivery": None,
        "stage2": None,
        "result": None,
        "stage2_result": None,
        "translations": {},
    }


async def test_a_done_job_reads_back_its_result_while_held(
    client, redis_fakes: RedisFakes
) -> None:
    job_id = await _pushed(client)
    await redis_fakes.jobs.hset(
        job_key("tenant-a", job_id),
        mapping={
            "status": "done",
            "reason": "voicemail",
            "delivery": "delivery_failed",
            "stage2": "not_eligible",
        },
    )
    await store_result("tenant-a", job_id, {"stage": 1}, ttl_seconds=60)

    body = (await client.get(f"{PUSH_URL}/{job_id}", headers=_headers())).json()
    assert (body["status"], body["reason"], body["delivery"], body["result"]) == (
        "done",
        "voicemail",
        "delivery_failed",
        {"stage": 1},
    )
    assert body["stage2"] == "not_eligible"
    assert body["stage2_result"] is None


async def test_a_done_stage2_reads_back_its_result_while_held(
    client, redis_fakes: RedisFakes, audit_log: io.StringIO
) -> None:
    job_id = await _pushed(client)
    await redis_fakes.jobs.hset(
        job_key("tenant-a", job_id), mapping={"status": "done", "stage2": "done"}
    )
    await store_stage2_result("tenant-a", job_id, {"stage": 2}, ttl_seconds=60)

    body = (await client.get(f"{PUSH_URL}/{job_id}", headers=_headers())).json()
    assert (body["stage2"], body["stage2_result"]) == ("done", {"stage": 2})
    assert [line["stage2_result_held"] for line in _reads(audit_log)] == [True]


async def test_another_tenants_job_is_404(client, audit_log: io.StringIO) -> None:
    """The guard: tenant-b's gates and Host cannot read tenant-a's job."""
    job_id = await _pushed(client)
    response = await client.get(
        f"{PUSH_URL}/{job_id}", headers=_headers(host="tenant-b")
    )
    assert (response.status_code, response.json()["reason"]) == (
        404,
        "call_job_not_found",
    )
    (line,) = _reads(audit_log)
    assert (line["tenant"], line["found"]) == ("tenant-b", False)


async def test_every_read_is_one_audit_line_with_no_content(
    client, redis_fakes: RedisFakes, audit_log: io.StringIO
) -> None:
    """The guard: who read which job and in what state, never what it holds."""
    job_id = await _pushed(client)
    await store_result(
        "tenant-a", job_id, {"transcript": TRANSCRIPT_WORDS}, ttl_seconds=60
    )
    for _ in range(2):
        await client.get(f"{PUSH_URL}/{job_id}", headers=_headers())

    lines = _reads(audit_log)
    assert len(lines) == 2
    assert {
        (line["logger"], line["tenant"], line["job_id"], line["found"], line["status"])
        for line in lines
    } == {("dodeal_ai.audit", "tenant-a", job_id, True, "queued")}
    assert all(line["result_held"] is True and line["request_id"] for line in lines)
    for line in lines:
        text = json.dumps(line)
        assert TRANSCRIPT_WORDS not in text and "SIGNED-LINK" not in text


async def test_reading_moves_the_reads_counter_only(
    client, redis_fakes: RedisFakes
) -> None:
    job_id = await _pushed(client)
    before = dict(redis_fakes.cost.store)
    await client.get(f"{PUSH_URL}/{job_id}", headers=_headers())
    moved = {
        key for key, value in redis_fakes.cost.store.items() if before.get(key) != value
    }
    assert moved == {"cost:reads:tenant:tenant-a"}


async def test_a_user_token_is_401_and_a_malformed_id_is_422(client) -> None:
    job_id = await _pushed(client)
    user = tokens.mint_token()
    assert (
        await client.get(f"{PUSH_URL}/{job_id}", headers=_headers(user))
    ).status_code == 401
    assert (
        await client.get(f"{PUSH_URL}/not-a-job", headers=_headers())
    ).status_code == 422


async def test_a_dead_job_store_is_503(client, monkeypatch) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    dead = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(jobs, "get_jobs_client", lambda: dead)
    response = await client.get(f"{PUSH_URL}/{'a' * 32}", headers=_headers())
    assert (response.status_code, response.json()["reason"]) == (
        503,
        "job_store_unavailable",
    )
