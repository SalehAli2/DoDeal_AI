"""The refusals and store failures of the modules the lead floored at 100
(passes, translation, reanalysis): each path a request or a task can take
when the job, the store or the model is not there."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from dodeal_ai.core.errors import (
    CallsNotEnabled,
    JobStoreUnavailableResponse,
)
from dodeal_ai.core.jobs import JobStoreUnavailable, read_translation
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence import reanalysis, translation
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.passes import CallClock
from dodeal_ai.units.call_intelligence.reanalysis import admit_reanalysis
from dodeal_ai.units.call_intelligence.schemas import ReanalysisRequest
from dodeal_ai.units.call_intelligence.translation import (
    Translated,
    check_translation,
    translate_call,
)
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit import test_call_reanalysis as reanalysis_case
from tests.unit import test_call_translation as case
from tests.unit.test_call_stage1 import _resolve
from tests.unit.test_translation_requests import GOOD, TRANSCRIPT

client = case.client


# --- passes ----------------------------------------------------------------------


def test_a_segment_of_a_call_with_no_recorded_time_has_no_said_at() -> None:
    assert CallClock(None, "Asia/Dubai").said_at(12.0) is None


# --- translation: the request ------------------------------------------------------


async def test_a_translation_for_no_job_is_404(
    client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    missing = await case._post(client, "ar")
    assert (missing.status_code, missing.json()["reason"]) == (
        404,
        "call_job_not_found",
    )


async def test_a_store_down_at_the_request_is_503(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(*args: Any) -> None:
        raise JobStoreUnavailable()

    monkeypatch.setattr(translation, "read_job", down)
    refused = await case._post(client, "ar")
    assert (refused.status_code, refused.json()["reason"]) == (
        503,
        "job_store_unavailable",
    )


def test_a_chunk_in_the_wrong_script_is_rejected() -> None:
    (run,) = translation.chunks(
        translation.CallText.of(case.STORED, country_code="971")
    )
    english = Translated.model_validate(
        {"segments": [{"segment": f"s{n + 1}", "text": "Hello"} for n in run]}
    )
    with pytest.raises(OutputValidationError):
        check_translation(run, "ar")(english)


# --- translation: the task ---------------------------------------------------------


async def test_a_task_for_a_gone_job_holds_and_sends_nothing(
    redis_fakes: RedisFakes,
) -> None:
    llm = FakeLLM()
    await translate_call({"llm": llm}, "tenant-a", case.JOB, "ar")
    assert llm.call_count == 0
    assert await read_translation("tenant-a", case.JOB, "ar") is None


async def _run(llm: FakeLLM, monkeypatch: pytest.MonkeyPatch, owe: Any) -> list[str]:
    """translate_call with a callback and `owe` as the delivery claim; the
    events sent."""
    sent: list[str] = []
    resolve_config = translation.resolve_calls_config

    async def with_callback(tenant: str) -> CallsConfig:
        config = await resolve_config(tenant)
        return config.model_copy(update={"callback_url": "https://crm.invalid/hook"})

    async def deliver(job: Any, event: str, config: CallsConfig) -> None:
        sent.append(event)

    monkeypatch.setattr(translation, "resolve_calls_config", with_callback)
    monkeypatch.setattr(translation, "owe_translation_delivery", owe)
    async with httpx.AsyncClient() as http:
        ctx = {"llm": llm, "http": http, "resolve": _resolve, "deliver": deliver}
        await translate_call(ctx, "tenant-a", case.JOB, "ar")
    return sent


async def test_a_malformed_chunk_fails_the_translation_after_its_reprompt(
    redis_fakes: RedisFakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    await case._done_call(TRANSCRIPT)
    bad = {"segments": GOOD["segments"][:1]}

    async def owed(*args: Any, **kwargs: Any) -> bool:
        return True

    llm = FakeLLM(json_response(bad), json_response(bad))
    assert await _run(llm, monkeypatch, owed) == ["call.translation:ar"]
    held = await read_translation("tenant-a", case.JOB, "ar")
    assert held is not None and held["reason"] == "translate_malformed_output"
    assert llm.call_count == 2


async def test_a_delivery_already_settled_sends_nothing_more(
    redis_fakes: RedisFakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    await case._done_call(TRANSCRIPT)

    async def gone(*args: Any, **kwargs: Any) -> bool:
        return False

    assert await _run(FakeLLM(json_response(GOOD)), monkeypatch, gone) == []


async def test_a_store_down_at_the_delivery_claim_is_logged_and_sends_nothing(
    redis_fakes: RedisFakes,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await case._done_call(TRANSCRIPT)

    async def down(*args: Any, **kwargs: Any) -> bool:
        raise JobStoreUnavailable()

    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_b"):
        sent = await _run(FakeLLM(json_response(GOOD)), monkeypatch, down)
    assert sent == []
    (line,) = [
        r for r in caplog.records if r.getMessage() == "call_translation_not_sent"
    ]
    assert line.__dict__["reason_code"] == "translate_store_unavailable"
    held = await read_translation("tenant-a", case.JOB, "ar")
    assert held is not None and held["reason"] is None


# --- reanalysis ----------------------------------------------------------------------


def _request() -> ReanalysisRequest:
    return ReanalysisRequest.model_validate(reanalysis_case._body())


async def test_a_reanalysis_with_calls_off_is_refused(redis_fakes: RedisFakes) -> None:
    off = reanalysis_case.CONFIG.model_copy(update={"calls_enabled": False})
    with pytest.raises(CallsNotEnabled):
        await admit_reanalysis(
            reanalysis_case.SCOPE, _request(), off, now=datetime.now(UTC)
        )


@pytest.mark.parametrize("fails", ["create_job", "read_job"])
async def test_a_store_that_fails_or_loses_the_job_is_503(
    redis_fakes: RedisFakes, monkeypatch: pytest.MonkeyPatch, fails: str
) -> None:
    async def down(*args: Any, **kwargs: Any) -> None:
        raise JobStoreUnavailable()

    async def lost(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(reanalysis, fails, down if fails == "create_job" else lost)
    with pytest.raises(JobStoreUnavailableResponse):
        await admit_reanalysis(
            reanalysis_case.SCOPE,
            _request(),
            reanalysis_case.CONFIG,
            now=datetime.now(UTC),
        )
