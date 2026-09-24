"""Translating a done call (translation.py): segment ids, speakers and times
kept, only the text translated; nothing to translate is a 409."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import fakeredis
import httpx
import pytest
from arq.connections import ArqRedis

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.jobs import create_job, read_translation, store_result
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence import queues
from dodeal_ai.units.call_intelligence.config import UNIT_B_SECTION, new_config_version
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from dodeal_ai.units.call_intelligence.translation import (
    Translated,
    check_translation,
    chunks,
    translate_call,
)
from tests.conftest import RedisFakes
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_stage1 import ON, SEGMENTS, _resolve

JOB = "ab" * 16
STORED = Transcript.of(SEGMENTS, provider="gemini", model="gemini-3.5-transcribe")
ARABIC = ["صباح الخير", "أريد فيلا", "راسلني على رقمي", "هل نلتقي يوم الثلاثاء؟"]


async def _done_call(result: dict[str, object] | None) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        ON,
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )
    metadata = {"author_id": 27, "lead_id": 1656, "duration_seconds": 150}
    await create_job(
        "tenant-a",
        7,
        job_id=JOB,
        request_id="req-t",
        queue=NORMAL_QUEUE,
        metadata=metadata,
        now=datetime.now(UTC),
    )
    if result is not None:
        await store_result("tenant-a", JOB, result, ttl_seconds=600)


async def test_segment_ids_speakers_and_times_survive_the_translation(
    redis_fakes: RedisFakes,
) -> None:
    await _done_call({"transcript": STORED.model_dump(mode="json")})
    answer = {
        "segments": [
            {"segment": f"s{n + 1}", "text": text} for n, text in enumerate(ARABIC)
        ]
    }
    async with httpx.AsyncClient() as http:
        ctx = {"llm": FakeLLM(json_response(answer)), "http": http, "resolve": _resolve}
        await translate_call(ctx, "tenant-a", JOB, "ar")
    held = await read_translation("tenant-a", JOB, "ar")
    assert held is not None and held["reason"] is None
    assert [
        (s["segment"], s["start_s"], s["speaker"], s["text"]) for s in held["segments"]
    ] == [
        (f"s{n + 1}", segment.start_s, "agent" if n % 2 == 0 else "client", ARABIC[n])
        for n, segment in enumerate(SEGMENTS)
    ]
    call = CallText.of(STORED, country_code="971")
    (run,) = chunks(call)
    with pytest.raises(OutputValidationError):
        check_translation(run, "ar")(
            Translated.model_validate({"segments": answer["segments"][:3]})
        )


@pytest.fixture
async def client(monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    get_settings.cache_clear()


def _post(client: httpx.AsyncClient, target: str) -> Any:
    return client.post(
        f"/api/v1/calls/jobs/{JOB}/translation",
        json={"target": target},
        headers={
            "Authorization": f"Bearer {tokens.mint_service_token()}",
            "Host": "tenant-a.dodealcrm.com",
        },
    )


async def test_an_expired_result_is_409_and_so_is_the_calls_own_language(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(None)
    expired = await _post(client, "ar")
    assert (expired.status_code, expired.json()["reason"]) == (409, "result_expired")
    await store_result(
        "tenant-a", JOB, {"transcript": STORED.model_dump(mode="json")}, ttl_seconds=60
    )
    same = await _post(client, "en")
    assert (same.status_code, same.json()["reason"]) == (409, "already_in_language")
    queued = await _post(client, "ar")
    assert queued.status_code == 202
    assert queued.json() == {"job_id": JOB, "target": "ar", "status": "queued"}


async def test_a_queue_that_is_down_is_queue_unavailable(monkeypatch) -> None:
    server = fakeredis.FakeServer()
    server.connected = False
    down = ArqRedis(
        connection_pool=fakeredis.FakeAsyncRedis(server=server).connection_pool
    )
    monkeypatch.setattr(queues, "get_queue_client", lambda: down)
    with pytest.raises(QueueUnavailable):
        await queues.enqueue_translation("tenant-a", JOB, "ar")
