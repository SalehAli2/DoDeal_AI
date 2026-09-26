"""POST /api/v1/calls/jobs/{job_id}/whatsapp (whatsapp.py): the suggestion
written again in the language asked, from the stored stage-1 result; paid
once per language, held no longer than the call, readable by the status
route."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.cost.limiter import CallsBudgetPaused
from dodeal_ai.core.errors import ModelUnavailableError
from dodeal_ai.core.jobs import (
    JobStoreUnavailable,
    claim_whatsapp,
    create_job,
    store_result,
    whatsapp_claim_key,
    whatsapp_key,
)
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.main import app
from dodeal_ai.units.call_intelligence import whatsapp
from dodeal_ai.units.call_intelligence.config import UNIT_B_SECTION, new_config_version
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE
from dodeal_ai.units.call_intelligence.transcriber import Transcript
from tests.conftest import RedisFakes
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_stage1 import ON, SEGMENTS

JOB = "cd" * 16
STORED = Transcript.of(SEGMENTS, provider="fake", model="fake-stt-1")
RESULT = {"transcript": STORED.model_dump(mode="json")}

RUSSIAN = "Спасибо за ваше время, до встречи во вторник."
EGYPTIAN = "شكرا على وقتك، نشوفك يوم التلات إن شاء الله."
ENGLISH = "Thank you for your time, see you on Tuesday."


async def _done_call(result: dict[str, object] | None, *, ttl: int = 600) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        ON,
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )
    await create_job(
        "tenant-a",
        7,
        job_id=JOB,
        request_id="req-w",
        queue=NORMAL_QUEUE,
        metadata={"author_id": 27, "lead_id": 1656, "duration_seconds": 150},
        now=datetime.now(UTC),
    )
    if result is not None:
        await store_result("tenant-a", JOB, result, ttl_seconds=ttl)


@pytest.fixture
async def client(monkeypatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.pop(get_llm_client, None)
    get_settings.cache_clear()


def _answering(*answers: Any) -> FakeLLM:
    llm = FakeLLM(
        *(a if isinstance(a, BaseException) else json_response(a) for a in answers)
    )
    app.dependency_overrides[get_llm_client] = lambda: llm
    return llm


def _headers(host: str = "tenant-a") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token(subdomain=host)}",
        "Host": f"{host}.dodealcrm.com",
    }


def _post(client: httpx.AsyncClient, language: str, **headers: str) -> Any:
    return client.post(
        f"/api/v1/calls/jobs/{JOB}/whatsapp",
        json={"language": language},
        headers=_headers(**headers),
    )


# --- the guard -----------------------------------------------------------------------


async def test_an_expired_result_is_409_and_pays_for_nothing(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(None)
    llm = _answering({"whatsapp": RUSSIAN})
    expired = await _post(client, "ru")
    assert (expired.status_code, expired.json()["reason"]) == (409, "result_expired")
    await store_result("tenant-a", JOB, {"transcript": None}, ttl_seconds=600)
    untranscribed = await _post(client, "ru")
    assert (untranscribed.status_code, untranscribed.json()["reason"]) == (
        409,
        "result_expired",
    )
    assert llm.call_count == 0


@pytest.mark.parametrize(
    ("language", "text", "dialect"),
    [("ru", RUSSIAN, None), ("egyptian_ar", EGYPTIAN, "egyptian_ar"), ("en", ENGLISH, None)],
)  # fmt: skip
async def test_the_language_asked_is_honoured(
    client: httpx.AsyncClient,
    redis_fakes: RedisFakes,
    language: str,
    text: str,
    dialect: str | None,
) -> None:
    await _done_call(RESULT)
    llm = _answering({"whatsapp": text})
    answered = await _post(client, language)
    assert answered.status_code == 200
    assert answered.json() == {
        "job_id": JOB,
        "language": language,
        "dialect": dialect,
        "text": text,
    }
    (sent,) = llm.calls
    assert sent.prompt.variable.rstrip().endswith(
        f"WHATSAPP LANGUAGE: {language}\n----- END CALLER DATA -----"
    )


async def test_a_message_in_another_language_is_reprompted_then_503(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT)
    llm = _answering({"whatsapp": ENGLISH}, {"whatsapp": ENGLISH})
    refused = await _post(client, "ru")
    assert (refused.status_code, refused.json()["reason"]) == (503, "malformed_output")
    assert llm.call_count == 2
    assert await redis_fakes.jobs.get(whatsapp_key("tenant-a", JOB, "ru")) is None
    assert await redis_fakes.jobs.get(whatsapp_claim_key("tenant-a", JOB, "ru")) is None


# --- paid once -------------------------------------------------------------------------


async def test_a_held_suggestion_is_answered_again_and_read_by_status(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT)
    llm = _answering({"whatsapp": RUSSIAN})
    first = await _post(client, "ru")
    again = await _post(client, "ru")
    assert first.json() == again.json()
    assert llm.call_count == 1
    status = await client.get(f"/api/v1/calls/jobs/{JOB}", headers=_headers())
    held = status.json()["whatsapp"]
    assert list(held) == ["ru"]
    assert (held["ru"]["text"], held["ru"]["dialect"]) == (RUSSIAN, None)
    assert held["ru"]["versions"] == {"prompt": "unit_b_prompts_v13"}


async def test_a_language_being_written_is_409_and_pays_for_nothing(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT)
    assert await claim_whatsapp("tenant-a", JOB, "ru", ttl_seconds=60)
    llm = _answering({"whatsapp": RUSSIAN})
    busy = await _post(client, "ru")
    assert (busy.status_code, busy.json()["reason"]) == (409, "whatsapp_in_progress")
    assert llm.call_count == 0


async def test_an_unanswered_call_is_asked_once_more_and_no_more(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT)
    llm = _answering(ModelUnavailableError(), {"whatsapp": RUSSIAN})
    assert (await _post(client, "ru")).status_code == 200
    assert llm.call_count == 2
    llm = _answering(ModelUnavailableError(), ModelUnavailableError())
    down = await _post(client, "tr")
    assert (down.status_code, down.json()["reason"]) == (503, "model_unavailable")
    assert llm.call_count == 2
    assert await redis_fakes.jobs.get(whatsapp_claim_key("tenant-a", JOB, "tr")) is None


async def test_the_suggestion_expires_with_its_call(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT, ttl=90)
    _answering({"whatsapp": RUSSIAN})
    assert (await _post(client, "ru")).status_code == 200
    left = await redis_fakes.jobs.ttl(whatsapp_key("tenant-a", JOB, "ru"))
    assert 0 < left <= 90


@pytest.mark.parametrize(
    ("paused", "status", "reason"),
    [
        ("token_budget_exceeded", 429, "token_budget_exceeded"),
        ("audio_budget_exceeded", 429, "token_budget_exceeded"),
        ("cost_store_unavailable", 503, "cost_store_unavailable"),
    ],
)
async def test_a_spent_or_unreadable_budget_pays_for_nothing(
    client: httpx.AsyncClient,
    redis_fakes: RedisFakes,
    monkeypatch,
    paused: str,
    status: int,
    reason: str,
) -> None:
    async def spent(scope) -> None:
        raise CallsBudgetPaused(paused)

    monkeypatch.setattr(whatsapp, "calls_budget_preflight", spent)
    await _done_call(RESULT)
    llm = _answering({"whatsapp": RUSSIAN})
    refused = await _post(client, "ru")
    assert (refused.status_code, refused.json()["reason"]) == (status, reason)
    assert llm.call_count == 0


# --- the rest ---------------------------------------------------------------------------


async def test_another_tenants_job_is_404(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT)
    llm = _answering({"whatsapp": RUSSIAN})
    other = await _post(client, "ru", host="tenant-b")
    assert (other.status_code, other.json()["reason"]) == (404, "call_job_not_found")
    assert llm.call_count == 0


@pytest.mark.parametrize("language", ["ar", "maghrebi_ar", "msa_ar", "other", "de"])
async def test_a_language_not_of_the_twelve_is_422(
    client: httpx.AsyncClient, redis_fakes: RedisFakes, language: str
) -> None:
    await _done_call(RESULT)
    _answering({"whatsapp": RUSSIAN})
    assert (await _post(client, language)).status_code == 422


async def test_the_text_is_never_logged(
    client: httpx.AsyncClient, redis_fakes: RedisFakes, caplog
) -> None:
    await _done_call(RESULT)
    _answering({"whatsapp": RUSSIAN})
    with caplog.at_level(logging.INFO):
        assert (await _post(client, "ru")).status_code == 200
    (line,) = [r for r in caplog.records if r.getMessage() == "call_whatsapp_written"]
    assert (line.job_id, line.language, line.held) == (JOB, "ru", True)
    assert not [r for r in caplog.records if "Спасибо" in str(r.__dict__)]


# --- the store failing -----------------------------------------------------------------


def _down(*args: object, **kwargs: object):
    raise JobStoreUnavailable()


@pytest.mark.parametrize("step", ["read_job", "claim_whatsapp"])
async def test_a_store_down_before_the_pass_is_503_and_pays_for_nothing(
    client: httpx.AsyncClient, redis_fakes: RedisFakes, monkeypatch, step: str
) -> None:
    await _done_call(RESULT)
    monkeypatch.setattr(whatsapp, step, _down)
    llm = _answering({"whatsapp": RUSSIAN})
    down = await _post(client, "ru")
    assert (down.status_code, down.json()["reason"]) == (503, "job_store_unavailable")
    assert llm.call_count == 0


@pytest.mark.parametrize("step", ["store_whatsapp", "release_whatsapp"])
async def test_a_store_down_after_the_pass_still_answers(
    client: httpx.AsyncClient, redis_fakes: RedisFakes, monkeypatch, step: str
) -> None:
    await _done_call(RESULT)
    monkeypatch.setattr(whatsapp, step, _down)
    llm = _answering({"whatsapp": RUSSIAN})
    answered = await _post(client, "ru")
    assert (answered.status_code, answered.json()["text"]) == (200, RUSSIAN)
    assert llm.call_count == 1


async def test_a_message_over_60_words_is_reprompted_then_503(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await _done_call(RESULT)
    long = {"whatsapp": " ".join(["слово"] * 61)}
    llm = _answering(long, long)
    refused = await _post(client, "ru")
    assert (refused.status_code, refused.json()["reason"]) == (503, "malformed_output")
    assert llm.call_count == 2
