"""POST /api/v1/calls/jobs (register item 50): the service chain only, the
tenant's calls counter, one job per call, the priority queue for the tenant's
priority statuses, and 403 while the tenant has calls off."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import AsyncIterator, Iterator

import fakeredis
import httpx
import pytest
from arq.connections import ArqRedis

from dodeal_ai.core import jobs
from dodeal_ai.core.breaker import reset_breakers
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import JobStatus, call_index_key, job_key, read_job
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence import queues
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, PRIORITY_QUEUE
from tests.conftest import RedisFakes
from tests.helpers import tokens

URL = "/api/v1/calls/jobs"
CALLS_CONFIG_URL = "/api/v1/admin/tenant-config/unit_b"
LINK = "https://audio.tenant-a.example/calls/7.wav?X-Signature=SIGNED-LINK-SECRET"
PHONE_HASH = "ab" * 32
VOICEPRINT = "dm9pY2VwcmludC1ieXRlcw=="
ON = {"calls_enabled": True, "audio_hosts": ["audio.tenant-a.example"]}


def _body(**changes: object) -> dict:
    body: dict[str, object] = {
        "call_id": 7,
        "lead_id": 1656,
        "author_id": 27,
        "duration_seconds": 95,
        "recorded_at": "2026-09-23T08:00:00+04:00",
        "audio_url": LINK,
        "audio_url_expires_at": "2026-09-23T10:00:00+04:00",
        "lead_status": "viewing",
        "lead_phone_hash": PHONE_HASH,
        "agent_voiceprint": VOICEPRINT,
    }
    body.update(changes)
    return {name: value for name, value in body.items() if value is not None}


def _headers(token: str | None = None, host: str = "tenant-a") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token or tokens.mint_service_token()}",
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


async def _calls_on(client: httpx.AsyncClient, **changes: object) -> None:
    response = await client.put(
        CALLS_CONFIG_URL, json={**ON, **changes}, headers=_headers()
    )
    assert response.status_code == 200


async def _queued(fakes: RedisFakes, queue: str) -> list[tuple]:
    return [
        (job.function, job.args)
        for job in await fakes.queue.queued_jobs(queue_name=queue)
    ]


@pytest.fixture
def json_log() -> Iterator[io.StringIO]:
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


# --- the guard -----------------------------------------------------------------


async def test_a_user_token_is_401(client, redis_fakes: RedisFakes) -> None:
    await _calls_on(client)
    user = tokens.mint_token()
    response = await client.post(URL, json=_body(), headers=_headers(user))
    assert response.status_code == 401
    assert await redis_fakes.jobs.keys("call_job:*") == []


async def test_calls_off_is_403_calls_not_enabled_and_admits_nothing(
    client, redis_fakes: RedisFakes
) -> None:
    response = await client.post(URL, json=_body(), headers=_headers())
    assert response.status_code == 403
    assert response.json()["reason"] == "calls_not_enabled"
    assert await redis_fakes.jobs.keys("*") == []
    assert await _queued(redis_fakes, NORMAL_QUEUE) == []


async def test_a_push_is_202_queued_and_on_the_normal_queue(
    client, redis_fakes: RedisFakes
) -> None:
    await _calls_on(client)
    response = await client.post(URL, json=_body(), headers=_headers())

    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"job_id", "status"} and body["status"] == "queued"
    job = await read_job("tenant-a", body["job_id"])
    assert job is not None and job.status is JobStatus.QUEUED
    assert (job.call_id, job.queue, job.metadata["author_id"]) == (7, NORMAL_QUEUE, 27)
    assert job.metadata["audio_url"] == LINK
    assert await _queued(redis_fakes, NORMAL_QUEUE) == [
        ("process_call", ("tenant-a", body["job_id"]))
    ]


async def test_a_duplicate_push_returns_the_first_job_id(
    client, redis_fakes: RedisFakes
) -> None:
    await _calls_on(client)
    first = (await client.post(URL, json=_body(), headers=_headers())).json()
    second = await client.post(
        URL, json=_body(lead_status="qualified"), headers=_headers()
    )

    assert second.status_code == 202
    assert second.json() == first
    assert len(await redis_fakes.jobs.keys("call_job:*")) == 1
    assert len(await _queued(redis_fakes, NORMAL_QUEUE)) == 1
    assert await _queued(redis_fakes, PRIORITY_QUEUE) == []


@pytest.mark.parametrize("status", ["Qualified", "negotiation"])
async def test_a_priority_status_is_routed_to_the_priority_queue(
    client, redis_fakes: RedisFakes, status: str
) -> None:
    await _calls_on(client)
    body = (
        await client.post(URL, json=_body(lead_status=status), headers=_headers())
    ).json()
    assert await _queued(redis_fakes, PRIORITY_QUEUE) == [
        ("process_call", ("tenant-a", body["job_id"]))
    ]
    assert await _queued(redis_fakes, NORMAL_QUEUE) == []


async def test_the_tenants_own_priority_list_decides(
    client, redis_fakes: RedisFakes
) -> None:
    await _calls_on(client, priority_statuses=["viewing"])
    await client.post(URL, json=_body(), headers=_headers())
    assert len(await _queued(redis_fakes, PRIORITY_QUEUE)) == 1


# --- the gate -------------------------------------------------------------------


async def test_only_the_calls_counter_moves(client, redis_fakes: RedisFakes) -> None:
    await _calls_on(client)
    reads_before = dict(redis_fakes.cost.store)
    await client.post(URL, json=_body(), headers=_headers())
    await client.post(URL, json=_body(call_id=8), headers=_headers())

    moved = {
        key: value
        for key, value in redis_fakes.cost.store.items()
        if reads_before.get(key) != value
    }
    assert moved == {"cost:calls:tenant:tenant-a": 2}


async def test_over_the_calls_cap_is_429(client, monkeypatch) -> None:
    await _calls_on(client)
    monkeypatch.setenv("DODEAL_COST_CALLS_PER_TENANT_LIMIT", "1")
    get_settings.cache_clear()
    assert (await client.post(URL, json=_body(), headers=_headers())).status_code == 202
    over = await client.post(URL, json=_body(call_id=8), headers=_headers())
    assert over.status_code == 429


async def test_another_tenants_host_is_403(client) -> None:
    response = await client.post(URL, json=_body(), headers=_headers(host="tenant-b"))
    assert response.status_code == 403


# --- the body -------------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"callback_url": "https://evil.example/hook"},
        {"transcript": "text"},
        {"audio_url": "http://audio.tenant-a.example/7.wav"},
        {"audio_url": "https://user:pw@audio.tenant-a.example/7.wav"},
        {"audio_url": "https://[::1/7.wav"},
        {"audio_url": "https://audio.example/" + "a" * 4096},
        {"recorded_at": "2026-09-23T08:00:00"},
        {"audio_url_expires_at": "2026-09-23T10:00:00"},
        {"call_id": 0},
        {"lead_id": 0},
        {"author_id": 0},
        {"duration_seconds": -1},
        {"language_hint": "fr"},
        {"call_outcome": "busy"},
        {"lead_phone_hash": "AB" * 32},
        {"agent_phone_hash": "ab" * 31},
        {"agent_voiceprint": "not base64!"},
        {"agent_voiceprint": "A" * 16_388},
    ],
)
async def test_a_malformed_body_is_422_and_admits_nothing(
    client, redis_fakes: RedisFakes, changes: dict
) -> None:
    await _calls_on(client)
    response = await client.post(URL, json=_body(**changes), headers=_headers())
    assert response.status_code == 422
    assert response.json()["reason"] == "invalid_request"
    assert "SIGNED-LINK-SECRET" not in response.text
    assert await redis_fakes.jobs.keys("call_job:*") == []


async def test_the_optional_fields_are_optional(client) -> None:
    await _calls_on(client)
    minimal = {
        name: value
        for name, value in _body().items()
        if name not in {"lead_status", "lead_phone_hash", "agent_voiceprint"}
    }
    response = await client.post(URL, json=minimal, headers=_headers())
    assert response.status_code == 202


async def test_an_explicit_null_voiceprint_is_no_voiceprint(client) -> None:
    await _calls_on(client, voice_id_enabled=True)
    body = {**_body(), "agent_voiceprint": None}
    accepted = (await client.post(URL, json=body, headers=_headers())).json()
    job = await read_job("tenant-a", accepted["job_id"])
    assert job is not None and "agent_voiceprint" not in job.metadata


async def test_the_voiceprint_is_kept_only_under_the_voice_id_switch(client) -> None:
    await _calls_on(client)
    off = (await client.post(URL, json=_body(), headers=_headers())).json()
    await _calls_on(client, voice_id_enabled=True)
    on = (await client.post(URL, json=_body(call_id=8), headers=_headers())).json()

    kept_off = await read_job("tenant-a", off["job_id"])
    kept_on = await read_job("tenant-a", on["job_id"])
    assert kept_off is not None and "agent_voiceprint" not in kept_off.metadata
    assert kept_on is not None and kept_on.metadata["agent_voiceprint"] == VOICEPRINT


# --- the stores fail closed -----------------------------------------------------


async def test_a_dead_job_store_is_503(client, monkeypatch) -> None:
    await _calls_on(client)
    server = fakeredis.FakeServer()
    server.connected = False
    dead = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    monkeypatch.setattr(jobs, "get_jobs_client", lambda: dead)
    response = await client.post(URL, json=_body(), headers=_headers())
    assert (response.status_code, response.json()["reason"]) == (
        503,
        "job_store_unavailable",
    )


async def test_an_index_that_outlived_its_job_is_503(
    client, redis_fakes: RedisFakes
) -> None:
    await _calls_on(client)
    await redis_fakes.jobs.set(call_index_key("tenant-a", 7), "gone")
    response = await client.post(URL, json=_body(), headers=_headers())
    assert (response.status_code, response.json()["reason"]) == (
        503,
        "job_store_unavailable",
    )


async def test_a_dead_queue_is_503_and_the_next_push_puts_the_job_on_it(
    client, redis_fakes: RedisFakes, monkeypatch
) -> None:
    """The job is kept, queued; pushing the call again repairs the queue."""
    await _calls_on(client)
    server = fakeredis.FakeServer()
    server.connected = False
    dead = ArqRedis(
        connection_pool=fakeredis.FakeAsyncRedis(server=server).connection_pool
    )
    monkeypatch.setattr(queues, "get_queue_client", lambda: dead)

    refused = await client.post(URL, json=_body(), headers=_headers())
    assert (refused.status_code, refused.json()["reason"]) == (503, "queue_unavailable")

    server.connected = True
    reset_breakers()
    healed = await client.post(URL, json=_body(), headers=_headers())
    assert healed.status_code == 202
    assert [
        (job.function, job.args)
        for job in await dead.queued_jobs(queue_name=NORMAL_QUEUE)
    ] == [("process_call", ("tenant-a", healed.json()["job_id"]))]


async def test_a_job_already_running_is_not_queued_again(
    client, redis_fakes: RedisFakes
) -> None:
    await _calls_on(client)
    first = (await client.post(URL, json=_body(), headers=_headers())).json()
    await redis_fakes.jobs.hset(
        job_key("tenant-a", first["job_id"]), "status", "transcribing"
    )
    await redis_fakes.queue.flushall()

    again = await client.post(URL, json=_body(), headers=_headers())
    assert again.json() == {"job_id": first["job_id"], "status": "transcribing"}
    assert await _queued(redis_fakes, NORMAL_QUEUE) == []


async def test_no_line_carries_the_link_a_hash_or_the_voiceprint(
    client, json_log: io.StringIO
) -> None:
    await _calls_on(client, voice_id_enabled=True)
    await client.post(URL, json=_body(), headers=_headers())
    lines = [json.loads(line) for line in json_log.getvalue().splitlines()]
    admitted = [line for line in lines if line["message"] == "call_job_admitted"]
    assert len(admitted) == 1 and admitted[0]["new_job"] is True
    text = json_log.getvalue()
    for secret in ("SIGNED-LINK-SECRET", PHONE_HASH, VOICEPRINT, "audio.tenant-a"):
        assert secret not in text
