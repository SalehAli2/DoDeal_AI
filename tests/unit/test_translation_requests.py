"""A translation request (translation.py, M9): a repeat answers from the held
translation and queues nothing; the calls budget is checked in the request and
again before any chunk is paid for; a hold that fails still sends the event;
the task keeps no arq result, so "queued" always has a run behind it."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from dodeal_ai.core.cost.limiter import CallsBudgetPaused
from dodeal_ai.core.jobs import JobStoreUnavailable, read_translation, store_translation
from dodeal_ai.units.call_intelligence import queues, translation
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.delivery import translation_event
from dodeal_ai.units.call_intelligence.queues import STAGE2_QUEUE
from dodeal_ai.units.call_intelligence.translation import translate_call
from dodeal_ai.workers import calls as calls_worker
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit import test_call_translation as case
from tests.unit.test_call_stage1 import _resolve

TRANSCRIPT = {"transcript": case.STORED.model_dump(mode="json")}
GOOD = {
    "segments": [{"segment": f"s{n + 1}", "text": t} for n, t in enumerate(case.ARABIC)]
}
HELD = {
    "target": "ar",
    "segments": [{"segment": "s1", "text": case.ARABIC[0]}],
    "reason": None,
    "versions": {"prompt": "unit_b_prompts_v19"},
}

client = case.client


@pytest.fixture
def enqueued(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str]]:
    """Every translation put on the queue, and nothing actually queued."""
    seen: list[tuple[str, str, str]] = []

    async def enqueue(tenant: str, job_id: str, target: str) -> None:
        seen.append((tenant, job_id, target))

    monkeypatch.setattr(translation, "enqueue_translation", enqueue)
    return seen


def _spent(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    async def spent(scope: object) -> None:
        raise CallsBudgetPaused(reason)

    monkeypatch.setattr(translation, "calls_budget_preflight", spent)


# --- the request ---------------------------------------------------------------


async def test_a_repeat_request_answers_from_the_held_translation(
    client: httpx.AsyncClient, enqueued: list[Any], redis_fakes: RedisFakes
) -> None:
    await case._done_call(TRANSCRIPT)
    await store_translation("tenant-a", case.JOB, "ar", HELD, ttl_seconds=60)
    held = await case._post(client, "ar")
    assert held.status_code == 202
    assert held.json() == {
        "job_id": case.JOB,
        "target": "ar",
        "status": "done",
        "translation": HELD,
    }
    assert enqueued == []


async def test_a_held_failure_is_queued_again(
    client: httpx.AsyncClient, enqueued: list[Any], redis_fakes: RedisFakes
) -> None:
    await case._done_call(TRANSCRIPT)
    failed = {**HELD, "segments": None, "reason": "translate_model_unavailable"}
    await store_translation("tenant-a", case.JOB, "ar", failed, ttl_seconds=60)
    queued = await case._post(client, "ar")
    assert (queued.status_code, queued.json()["status"]) == (202, "queued")
    assert enqueued == [("tenant-a", case.JOB, "ar")]


@pytest.mark.parametrize(
    ("paused", "status", "reason"),
    [
        ("token_budget_exceeded", 429, "token_budget_exceeded"),
        ("cost_store_unavailable", 503, "cost_store_unavailable"),
    ],
)
async def test_a_spent_or_unreadable_budget_queues_nothing(
    client: httpx.AsyncClient,
    enqueued: list[Any],
    redis_fakes: RedisFakes,
    monkeypatch: pytest.MonkeyPatch,
    paused: str,
    status: int,
    reason: str,
) -> None:
    _spent(monkeypatch, paused)
    await case._done_call(TRANSCRIPT)
    refused = await case._post(client, "ar")
    assert (refused.status_code, refused.json()["reason"]) == (status, reason)
    assert enqueued == []


# --- the task ------------------------------------------------------------------


async def _translate(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> list[tuple[str, str]]:
    """translate_call with a callback configured; the events it sent."""
    sent: list[tuple[str, str]] = []
    resolve_config = translation.resolve_calls_config

    async def with_callback(tenant: str) -> CallsConfig:
        config = await resolve_config(tenant)
        return config.model_copy(update={"callback_url": "https://crm.invalid/hook"})

    async def deliver(job: Any, event: str, config: CallsConfig) -> None:
        sent.append((job.job_id, event))

    monkeypatch.setattr(translation, "resolve_calls_config", with_callback)
    async with httpx.AsyncClient() as http:
        ctx = {"llm": llm, "http": http, "resolve": _resolve, "deliver": deliver}
        await translate_call(ctx, "tenant-a", case.JOB, "ar")
    return sent


async def test_the_task_checks_the_budget_before_paying(
    redis_fakes: RedisFakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    _spent(monkeypatch, "token_budget_exceeded")
    await case._done_call(TRANSCRIPT)
    llm = FakeLLM()
    sent = await _translate(llm, monkeypatch)
    assert llm.call_count == 0
    held = await read_translation("tenant-a", case.JOB, "ar")
    assert held is not None
    assert (held["segments"], held["reason"]) == (
        None,
        "translate_token_budget_exceeded",
    )
    assert sent == [(case.JOB, translation_event("ar"))]


@pytest.mark.parametrize("refusals", [1, 2])
async def test_a_hold_that_fails_still_sends_a_failure_event(
    redis_fakes: RedisFakes, monkeypatch: pytest.MonkeyPatch, refusals: int
) -> None:
    """Refused once, the failure is held in the answer's place; refused
    twice, nothing is held. The event goes either way, and nothing is paid
    for again."""
    await case._done_call(TRANSCRIPT)
    store = translation.store_translation
    left = [refusals]

    async def flaky(*args: Any, **kwargs: Any) -> None:
        if left[0]:
            left[0] -= 1
            raise JobStoreUnavailable()
        await store(*args, **kwargs)

    monkeypatch.setattr(translation, "store_translation", flaky)
    llm = FakeLLM(json_response(GOOD))
    sent = await _translate(llm, monkeypatch)
    assert llm.call_count == 1
    assert sent == [(case.JOB, translation_event("ar"))]
    held = await read_translation("tenant-a", case.JOB, "ar")
    if refusals == 1:
        assert held is not None
        assert (held["segments"], held["reason"]) == (
            None,
            "translate_store_unavailable",
        )
    else:
        assert held is None


async def test_the_task_keeps_no_arq_result() -> None:
    """A finished run leaves nothing that turns the next enqueue into a no-op."""
    built = calls_worker.worker_settings(STAGE2_QUEUE)
    (translate,) = [f for f in built["functions"] if f.name == queues.TRANSLATE_CALL]
    assert (translate.keep_result_s, translate.max_tries) == (0, 1)
