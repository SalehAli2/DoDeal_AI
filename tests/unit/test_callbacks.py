"""Signed callbacks (Unit B): the signature verifies over "<timestamp>.<body>",
they go to the tenant's callback_url and nowhere a push body names, a
dead-lettered job sends call.failed, and a failed delivery is retried at 60,
300, 1800 and 7200 s before the job fails with delivery_failed."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import pathlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from dodeal_ai.core import callbacks
from dodeal_ai.core.callbacks import (
    CALL_FAILED,
    CALL_STAGE1,
    CALL_STAGE2,
    EVENTS,
    Delivery,
    event_id,
    post_event,
)
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.jobs import (
    DeliveryState,
    JobStatus,
    Stage2State,
    create_job,
    read_job,
    read_stage2_result,
    settle_stage2,
)
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import sweep
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
    resolve_calls_config,
)
from dodeal_ai.units.call_intelligence.delivery import deliver_callback, deliver_event
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.prompts import (
    COACHING_TEMPLATE,
    ESCALATIONS_TEMPLATE,
    EXTRAS_TEMPLATE,
    OBJECTIONS_TEMPLATE,
)
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.stage2 import analyse_stage2
from dodeal_ai.units.call_intelligence.transcriber import Segment
from dodeal_ai.units.call_intelligence.worker import process_call
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.wave2_answers import coaching_answer, extras_answer

SECRET = "callback-secret-for-tenant-a"
AUDIO_HOST = "audio.tenant-a.example"
CRM_HOST = "crm.tenant-a.example"
CALLBACK = f"https://{CRM_HOST}/hooks/calls"
ADDRESSES = {AUDIO_HOST: "93.184.216.34", CRM_HOST: "93.184.216.35"}
JOB = "job-1"


class _World:
    """The audio host and the CRM, on one MockTransport: GETs are audio,
    POSTs are callbacks, and the CRM's answers are scripted."""

    def __init__(self) -> None:
        self.posts: list[httpx.Request] = []
        self.gets: list[httpx.Request] = []
        self.crm_status: list[int] = []
        self.audio_status = 200

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            self.gets.append(request)
            if self.audio_status != 200:
                return httpx.Response(self.audio_status)
            return httpx.Response(
                200,
                content=b"RIFF" + b"\x00" * 64,
                headers={"content-type": "audio/wav"},
            )
        self.posts.append(request)
        return httpx.Response(self.crm_status.pop(0) if self.crm_status else 204)


async def _resolve(host: str, port: int) -> list[str]:
    return [ADDRESSES[host]]


@pytest.fixture(autouse=True)
def _secret(monkeypatch) -> None:
    monkeypatch.setenv("DODEAL_CALL_CALLBACK_SECRETS", json.dumps({"tenant-a": SECRET}))
    get_settings.cache_clear()


@pytest.fixture
def world() -> _World:
    return _World()


@pytest.fixture
async def ctx(world: _World) -> AsyncIterator[dict[str, Any]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(world)) as http:
        context: dict[str, Any] = {
            "http": http,
            "transcriber": FakeTranscriber(),
            "resolve": _resolve,
        }
        context["deliver"] = lambda job, event, config: deliver_event(
            context, job, event, config
        )
        yield context


def _signed(request: httpx.Request) -> bool:
    timestamp = request.headers["x-dodeal-timestamp"]
    expected = hmac.new(
        SECRET.encode(), timestamp.encode() + b"." + request.content, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, request.headers["x-dodeal-signature"])


async def _setup(config: dict | None = None, **push: object) -> None:
    """The tenant's calls on with a callback, and one pushed call."""
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {
            "calls_enabled": True,
            "audio_hosts": [AUDIO_HOST],
            "callback_url": CALLBACK,
            **(config or {}),
        },
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
            "audio_url": f"https://{AUDIO_HOST}/7.wav?sig=SIGNED-LINK",
            "audio_url_expires_at": (
                datetime.now(UTC) + timedelta(hours=1)
            ).isoformat(),
            "lead_phone_hash": "ab" * 32,
            **push,
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


async def _job():
    job = await read_job("tenant-a", JOB)
    assert job is not None
    return job


# --- the guard ------------------------------------------------------------------


async def test_stage1_goes_signed_to_the_callback_url_only(
    ctx: dict, world: _World
) -> None:
    """The signature verifies, the POST goes to the configured host, and no
    URL from the push reaches the callback's body."""
    await _setup()
    await process_call(ctx, "tenant-a", JOB)

    (post,) = world.posts
    assert _signed(post)
    assert post.headers["host"] == CRM_HOST
    assert post.url.host == ADDRESSES[CRM_HOST]
    assert post.url.path == "/hooks/calls"
    assert post.extensions["sni_hostname"] == CRM_HOST
    assert post.headers["x-dodeal-event"] == CALL_STAGE1
    assert post.headers["x-dodeal-event-id"] == event_id("tenant-a", JOB, CALL_STAGE1)
    body = json.loads(post.content)
    assert body["result"]["transcript"]["segments"]
    assert (body["job_id"], body["call_id"], body["lead_id"]) == (JOB, 7, 1656)
    for secret in ("SIGNED-LINK", AUDIO_HOST, "ab" * 32):
        assert secret not in post.content.decode()
    job = await _job()
    assert (job.status, job.delivery) == (JobStatus.DONE, DeliveryState.DELIVERED)


async def test_a_dead_lettered_job_sends_call_failed_with_its_reason(
    ctx: dict, world: _World, monkeypatch
) -> None:
    await _setup()
    monkeypatch.setenv("DODEAL_CALL_MAX_TRIES", "1")
    get_settings.cache_clear()
    world.audio_status = 503

    await process_call(ctx, "tenant-a", JOB)

    (post,) = world.posts
    assert _signed(post) and post.headers["x-dodeal-event"] == CALL_FAILED
    body = json.loads(post.content)
    assert (body["status"], body["reason"]) == (
        "dead_letter",
        "audio_source_unavailable",
    )
    assert "result" not in body
    assert (await _job()).delivery is DeliveryState.DELIVERED


async def test_a_call_failed_retry_that_lands_settles_its_delivery(
    ctx: dict, world: _World, redis_fakes: RedisFakes, monkeypatch
) -> None:
    """call.failed rides the same schedule and the same delivery field."""
    await _setup()
    monkeypatch.setenv("DODEAL_CALL_MAX_TRIES", "1")
    get_settings.cache_clear()
    world.audio_status = 503
    world.crm_status = [500]

    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).delivery is DeliveryState.PENDING
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    await deliver_callback(ctx, *queued.args)

    job = await _job()
    assert (job.status, job.delivery) == (
        JobStatus.DEAD_LETTER,
        DeliveryState.DELIVERED,
    )


# --- the schedule ---------------------------------------------------------------


async def test_a_failed_delivery_is_retried_on_the_schedule_then_fails(
    ctx: dict, world: _World, redis_fakes: RedisFakes
) -> None:
    await _setup()
    world.crm_status = [500] * 5

    await process_call(ctx, "tenant-a", JOB)
    job = await _job()
    assert (job.status, job.delivery) == (JobStatus.DONE, DeliveryState.PENDING)

    delays = []
    for attempt in range(1, 5):
        (queued,) = [
            job
            for job in await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
            if job.args[3:] == (attempt,)
        ]
        assert queued.function == "deliver_callback"
        delays.append(
            round((queued.score - queued.enqueue_time.timestamp() * 1000) / 1000)
        )
        await deliver_callback(ctx, *queued.args)

    assert delays == [60, 300, 1800, 7200]
    assert len(world.posts) == 5 and all(_signed(post) for post in world.posts)
    assert {post.headers["x-dodeal-event-id"] for post in world.posts} == {
        event_id("tenant-a", JOB, CALL_STAGE1)
    }
    job = await _job()
    assert (job.status, job.reason, job.delivery) == (
        JobStatus.DONE,
        None,
        DeliveryState.DELIVERY_FAILED,
    )


async def test_a_retry_that_lands_marks_the_delivery_delivered(
    ctx: dict, world: _World, redis_fakes: RedisFakes
) -> None:
    await _setup()
    world.crm_status = [503]
    await process_call(ctx, "tenant-a", JOB)
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)

    await deliver_callback(ctx, *queued.args)

    job = await _job()
    assert (job.status, job.delivery) == (JobStatus.DONE, DeliveryState.DELIVERED)
    await deliver_callback(ctx, *queued.args)
    assert len(world.posts) == 2


async def test_a_lost_stage2_sends_call_failed_for_stage2_on_its_own_delivery(
    ctx: dict, world: _World, redis_fakes: RedisFakes
) -> None:
    """Signed, retried on stage 2's delivery; the job stays done, stage 1's
    delivery untouched."""
    await _setup()
    await process_call(ctx, "tenant-a", JOB)
    assert (await _job()).stage2 is Stage2State.PENDING
    later = datetime.now(UTC) + timedelta(hours=2.1)
    await redis_fakes.queue.delete(f"arq:job:tenant-a:{JOB}:stage2")
    assert await sweep.sweep(now=later, deliver=ctx["deliver"]) == 1
    await redis_fakes.queue.delete(f"arq:job:tenant-a:{JOB}:stage2:sweep")
    world.crm_status = [503]
    lost = later + timedelta(hours=2.1)
    assert await sweep.sweep(now=lost, deliver=ctx["deliver"]) == 1

    job = await _job()
    assert (job.status, job.delivery, job.stage2_delivery) == (
        JobStatus.DONE,
        DeliveryState.DELIVERED,
        DeliveryState.PENDING,
    )
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    assert queued.args == ("tenant-a", JOB, CALL_FAILED, 1)
    await deliver_callback(ctx, *queued.args)
    job = await _job()
    assert (job.delivery, job.stage2_delivery) == (
        DeliveryState.DELIVERED,
        DeliveryState.DELIVERED,
    )
    stage1, failed, retried = world.posts
    assert stage1.headers["x-dodeal-event"] == CALL_STAGE1
    assert failed.headers["x-dodeal-event"] == CALL_FAILED
    assert _signed(failed) and _signed(retried)
    body = json.loads(retried.content)
    assert "result" not in body
    assert {name: body[name] for name in ("stage", "status", "stage2", "reason")} == {
        "stage": 2,
        "status": "done",
        "stage2": "failed",
        "reason": "stage2_lost",
    }
    await deliver_callback(ctx, *queued.args)
    assert len(world.posts) == 3


async def test_stage2_goes_signed_as_call_stage2_on_its_own_delivery(
    ctx: dict, world: _World
) -> None:
    """Signed like stage 1, its own event id, the held stage-2 result as the
    body's result; stage 1's delivery untouched."""
    said = (
        Segment(
            start_s=0,
            end_s=5,
            speaker="agent",
            text="Good morning, calling about the viewing.",
            language="en",
            confidence=0.9,
        ),
        Segment(
            start_s=5,
            end_s=10,
            speaker="lead",
            text="Yes, I still want to see the flat.",
            language="en",
            confidence=0.9,
        ),
    )
    ctx["transcriber"] = FakeTranscriber(said)
    await _setup()
    await process_call(ctx, "tenant-a", JOB)
    llm = FakeLLM()
    llm.script_for(OBJECTIONS_TEMPLATE, json_response({"objections": []}))
    llm.script_for(ESCALATIONS_TEMPLATE, json_response({"escalations": []}))
    llm.script_for(COACHING_TEMPLATE, json_response(coaching_answer(said[0].text)))
    llm.script_for(EXTRAS_TEMPLATE, json_response(extras_answer()))
    await analyse_stage2({**ctx, "llm": llm}, "tenant-a", JOB)

    stage1, stage2 = world.posts
    assert stage1.headers["x-dodeal-event"] == CALL_STAGE1
    assert stage2.headers["x-dodeal-event"] == CALL_STAGE2
    assert stage2.headers["x-dodeal-event-id"] == event_id("tenant-a", JOB, CALL_STAGE2)
    assert _signed(stage2)
    body = json.loads(stage2.content)
    assert body["result"] == await read_stage2_result("tenant-a", JOB)
    assert body["result"]["stage"] == 2
    job = await _job()
    assert (job.status, job.stage2, job.delivery, job.stage2_delivery) == (
        JobStatus.DONE,
        Stage2State.DONE,
        DeliveryState.DELIVERED,
        DeliveryState.DELIVERED,
    )


async def test_a_stage2_call_failed_refused_fails_its_own_delivery_only(
    ctx: dict, world: _World, redis_fakes: RedisFakes, monkeypatch
) -> None:
    await _setup()
    await process_call(ctx, "tenant-a", JOB)
    await settle_stage2(
        await _job(),
        Stage2State.FAILED,
        now=datetime.now(UTC),
        reason="stage2_lost",
        delivery=DeliveryState.PENDING,
    )
    monkeypatch.delenv("DODEAL_CALL_CALLBACK_SECRETS")
    get_settings.cache_clear()
    config = await resolve_calls_config("tenant-a")
    assert not await deliver_event(ctx, await _job(), CALL_FAILED, config)
    job = await _job()
    assert (job.delivery, job.stage2_delivery) == (
        DeliveryState.DELIVERED,
        DeliveryState.DELIVERY_FAILED,
    )


async def test_a_voicemail_retried_to_done_keeps_its_label(
    ctx: dict, world: _World, redis_fakes: RedisFakes
) -> None:
    await _setup(call_outcome="voicemail")
    world.crm_status = [500]
    await process_call(ctx, "tenant-a", JOB)
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    await deliver_callback(ctx, *queued.args)
    job = await _job()
    assert (job.status, job.reason, job.delivery) == (
        JobStatus.DONE,
        "voicemail",
        DeliveryState.DELIVERED,
    )


async def test_with_no_secret_nothing_is_sent_and_the_delivery_fails_at_once(
    ctx: dict, world: _World, monkeypatch
) -> None:
    await _setup()
    monkeypatch.setenv("DODEAL_CALL_CALLBACK_SECRETS", "{}")
    get_settings.cache_clear()

    await process_call(ctx, "tenant-a", JOB)

    assert world.posts == []
    job = await _job()
    assert (job.status, job.reason, job.delivery) == (
        JobStatus.DONE,
        None,
        DeliveryState.DELIVERY_FAILED,
    )


async def test_a_callback_removed_while_a_retry_waits_fails_the_delivery(
    ctx: dict, world: _World, redis_fakes: RedisFakes
) -> None:
    """Nowhere left to send it: delivery_failed, and the job stays done."""
    await _setup()
    world.crm_status = [500]
    await process_call(ctx, "tenant-a", JOB)
    (queued,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    await _setup({"callback_url": None})

    await deliver_callback(ctx, *queued.args)

    job = await _job()
    assert (job.status, job.delivery) == (
        JobStatus.DONE,
        DeliveryState.DELIVERY_FAILED,
    )
    assert len(world.posts) == 1


async def test_a_refused_call_failed_leaves_the_job_as_it_was(
    ctx: dict, world: _World
) -> None:
    await _setup()
    job = await _job()
    config = parse_unit_b_section(
        {
            "calls_enabled": True,
            "audio_hosts": [AUDIO_HOST],
            "callback_url": "https://crm.tenant-a.example:8443/hooks",
        }
    )
    assert not await deliver_event(ctx, job, CALL_FAILED, config)
    assert world.posts == [] and (await _job()).status is JobStatus.QUEUED


async def test_with_no_callback_url_the_job_is_done_for_get(
    ctx: dict, world: _World
) -> None:
    await _setup({"callback_url": None})
    await process_call(ctx, "tenant-a", JOB)
    assert world.posts == []
    job = await _job()
    assert (job.status, job.delivery) == (JobStatus.DONE, None)


async def test_a_retry_for_a_job_that_moved_on_or_is_gone_sends_nothing(
    ctx: dict, world: _World
) -> None:
    await _setup()
    await deliver_callback(ctx, "tenant-a", "gone", CALL_STAGE1, 1)
    await deliver_callback(ctx, "tenant-a", JOB, CALL_STAGE1, 1)
    assert world.posts == []


# --- post_event on its own ----------------------------------------------------------


async def _post(world: _World, url: str = CALLBACK, resolve=_resolve) -> Delivery:
    async with httpx.AsyncClient(transport=httpx.MockTransport(world)) as http:
        return await post_event(
            url,
            tenant="tenant-a",
            event=CALL_STAGE1,
            event_id="e-1",
            body=b'{"x":1}',
            timestamp="1790000000",
            http=http,
            resolve=resolve,
        )


@pytest.mark.parametrize(
    "url",
    [
        f"http://{CRM_HOST}/hooks",
        f"https://{CRM_HOST}:8443/hooks",
        "https:///hooks",
        f"https://{CRM_HOST}:bad/hooks",
    ],
)
async def test_a_link_that_is_not_https_on_443_is_refused_unsent(
    world: _World, url: str
) -> None:
    assert await _post(world, url) is Delivery.REFUSED
    assert world.posts == []


async def test_a_private_address_is_refused_and_no_answer_is_retried(
    world: _World,
) -> None:
    async def private(host: str, port: int) -> list[str]:
        return ["10.0.0.7"]

    async def nothing(host: str, port: int) -> list[str]:
        return []

    assert await _post(world, resolve=private) is Delivery.REFUSED
    assert await _post(world, resolve=nothing) is Delivery.RETRY
    assert world.posts == []


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (200, Delivery.DELIVERED),
        (204, Delivery.DELIVERED),
        (302, Delivery.RETRY),
        (500, Delivery.RETRY),
        (400, Delivery.RETRY),
    ],
)
async def test_the_crms_answer_decides(
    world: _World, status: int, outcome: Delivery
) -> None:
    world.crm_status = [status]
    assert await _post(world) is outcome
    assert len(world.posts) == 1


async def test_a_transport_failure_is_retried() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(down)) as http:
        outcome = await post_event(
            CALLBACK,
            tenant="tenant-a",
            event=CALL_STAGE1,
            event_id="e-1",
            body=b"{}",
            timestamp="1",
            http=http,
            resolve=_resolve,
        )
    assert outcome is Delivery.RETRY


def test_event_ids_are_stable_per_job_and_event() -> None:
    assert event_id("tenant-a", JOB, CALL_STAGE1) == event_id(
        "tenant-a", JOB, CALL_STAGE1
    )
    assert len({event_id("tenant-a", JOB, e) for e in EVENTS}) == 3
    assert event_id("tenant-b", JOB, CALL_STAGE1) != event_id(
        "tenant-a", JOB, CALL_STAGE1
    )
    assert CALL_STAGE2 == "call.stage2"


def test_the_callback_secrets_are_read_in_one_place() -> None:
    """One `get_secret_value()` in core/callbacks.py, and no other src module
    names the setting but the settings class."""
    src = pathlib.Path("src/dodeal_ai")
    readers = sorted(
        path.relative_to(src).as_posix()
        for path in src.rglob("*.py")
        if "call_callback_secrets" in path.read_text(encoding="utf-8")
    )
    assert readers == ["core/callbacks.py", "core/config.py"]
    tree = ast.parse((src / "core" / "callbacks.py").read_text(encoding="utf-8"))
    reads = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == "get_secret_value"
    ]
    assert len(reads) == 1
    assert callbacks.DELIVERY_DELAYS_SECONDS == (60, 300, 1800, 7200)


async def test_a_retry_that_cannot_be_queued_is_queue_unavailable(monkeypatch) -> None:
    import fakeredis
    from arq.connections import ArqRedis

    from dodeal_ai.core.errors import QueueUnavailable
    from dodeal_ai.units.call_intelligence import queues

    server = fakeredis.FakeServer()
    server.connected = False
    dead = ArqRedis(
        connection_pool=fakeredis.FakeAsyncRedis(server=server).connection_pool
    )
    monkeypatch.setattr(queues, "get_queue_client", lambda: dead)
    with pytest.raises(QueueUnavailable):
        await queues.enqueue_delivery(
            "tenant-a",
            JOB,
            CALL_STAGE1,
            attempt=1,
            queue=NORMAL_QUEUE,
            defer=timedelta(seconds=60),
        )
