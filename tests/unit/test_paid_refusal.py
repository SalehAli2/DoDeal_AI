"""A breaker refusal (paid.py, M5, D-82): an open model breaker refuses a
pass's call with nothing sent. That never uses up a start: the start is given
back and the job pauses with the outage backoff, stage 1 or stage 2."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from arq import Retry

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import ModelUnavailableError
from dodeal_ai.core.jobs import JobStatus, Stage2State, read_job, read_work
from dodeal_ai.core.llm import LLMErrorReason, LLMProviderError
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.units.call_intelligence import stage2
from dodeal_ai.units.call_intelligence.paid import PASS_TRIES, PassRefused, PassUsage
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE
from dodeal_ai.units.call_intelligence.stage2 import analyse_stage2
from dodeal_ai.units.call_intelligence.worker import (
    MODEL_REFUSED,
    OUTAGE_DELAY_SECONDS,
    pause_delay,
    process_call,
)
from dodeal_ai.units.structured_intelligence.llm_call import (
    ModelRefused,
    complete_once,
)
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.scopes import TEST_SCOPE
from tests.unit import test_call_stage1 as stage1_case
from tests.unit import test_call_stage2 as stage2_case
from tests.unit.test_paid_reprompt import BAD, GOOD, NAME, _job, _run, _tails
from tests.unit.test_process_call import _resume_delays

ctx = stage1_case.ctx


def _refused() -> LLMProviderError:
    return LLMProviderError(LLMErrorReason.BREAKER_OPEN, transient=True)


# --- the seam: only an open breaker is a refusal -----------------------------------


@pytest.mark.parametrize(
    ("reason", "refused"),
    [(LLMErrorReason.BREAKER_OPEN, True), (LLMErrorReason.UNAVAILABLE, False)],
)
async def test_only_an_open_breaker_is_a_refusal(
    reason: LLMErrorReason, refused: bool
) -> None:
    llm = FakeLLM(LLMProviderError(reason, transient=True))
    with pytest.raises(ModelUnavailableError) as lost:
        await complete_once(
            llm,
            AssembledPrompt(stable="s", variable="v"),
            "call.extract",
            scope=TEST_SCOPE,
            settings=get_settings(),
            profile="unit_b.extract",
            max_output_tokens=100,
        )
    assert isinstance(lost.value, ModelRefused) is refused
    assert (lost.value.reason_code, lost.value.http_status) == (
        "model_unavailable",
        503,
    )


# --- a wave's pass -----------------------------------------------------------------


async def test_a_refusal_never_uses_up_a_start(redis_fakes: RedisFakes) -> None:
    """The guard: refused more times than a pass may start, then answered on
    its first counted start."""
    job = await _job()
    for _ in range(PASS_TRIES + 1):
        with pytest.raises(PassRefused):
            await _run(job, FakeLLM(_refused()), PassUsage())
        kept = await read_job(job.tenant, job.job_id)
        assert kept is not None and kept.passes.get(NAME, 0) == 0
        assert NAME not in await read_work(job.tenant, job.job_id)
    llm = FakeLLM(json_response(GOOD))
    await _run(job, llm, PassUsage())
    kept = await read_job(job.tenant, job.job_id)
    assert kept is not None and kept.passes[NAME] == 1
    assert llm.call_count == 1


async def test_a_refusal_after_a_lost_answer_gives_back_only_its_own_start(
    redis_fakes: RedisFakes,
) -> None:
    """The first start's request went and got nothing: that start stays used."""
    job = await _job()
    with pytest.raises(PassRefused):
        await _run(job, FakeLLM(ModelUnavailableError(), _refused()), PassUsage())
    kept = await read_job(job.tenant, job.job_id)
    assert kept is not None and kept.passes[NAME] == 1


async def test_a_refused_reprompt_goes_the_lost_reprompt_way(
    redis_fakes: RedisFakes,
) -> None:
    """An answer arrived: a pause would drop it, so the reprompt is sent once
    more alone and the first answer is never paid for again."""
    job = await _job()
    llm = FakeLLM(json_response(BAD), _refused(), json_response(GOOD))
    await _run(job, llm, PassUsage())
    assert _tails(llm) == [False, True, True]
    kept = await read_job(job.tenant, job.job_id)
    assert kept is not None and kept.passes[NAME] == 2


# --- stage 1 and stage 2 pause with the outage backoff ----------------------------


def test_a_refusal_backs_off_like_an_outage() -> None:
    delays = [pause_delay(MODEL_REFUSED, n, window_seconds=86_400) for n in (1, 2, 9)]
    assert delays == [300, 600, 3600]


async def test_stage1_refused_pauses_and_resumes_with_no_start_used(
    ctx: dict, redis_fakes: RedisFakes
) -> None:
    await stage1_case._push()
    ctx["llm"] = FakeLLM(_refused())
    await process_call(ctx, "tenant-a", stage1_case.JOB)

    job = await read_job("tenant-a", stage1_case.JOB)
    assert job is not None
    assert (job.status, job.reason, job.pauses, job.outages, job.attempts) == (
        JobStatus.PAUSED_BUDGET,
        MODEL_REFUSED,
        1,
        1,
        1,
    )
    assert job.passes.get("extract", 0) == 0
    (resumed,) = await redis_fakes.queue.queued_jobs(queue_name=NORMAL_QUEUE)
    (waited,) = _resume_delays([resumed])
    assert OUTAGE_DELAY_SECONDS - 5 <= waited <= OUTAGE_DELAY_SECONDS + 5

    ctx["llm"] = stage1_case._good_llm()
    await process_call(ctx, "tenant-a", stage1_case.JOB)
    job = await read_job("tenant-a", stage1_case.JOB)
    assert job is not None
    assert (job.status, job.passes) == (JobStatus.DONE, {"extract": 1, "prose": 1})
    # The transcript was kept: the resume paid for no second transcription.
    assert len(ctx["transcriber"].calls) == 1


@pytest.fixture
async def stage2_ctx() -> AsyncIterator[dict[str, Any]]:
    """Stage 1 with no model, as test_call_stage2's own ctx."""
    transport = httpx.MockTransport(stage2_case._audio)
    async with httpx.AsyncClient(transport=transport) as http:
        yield {
            "http": http,
            "transcriber": stage2_case.FakeTranscriber(stage2_case.SEGMENTS),
            "resolve": stage2_case._resolve,
            "llm": None,
        }


async def test_stage2_refused_runs_again_then_fails_only_on_its_last_run(
    stage2_ctx: dict,
) -> None:
    await stage2_case._done_and_pending(stage2_ctx)
    for job_try, delay in ((1, 300), (2, 600)):
        refused = {"llm": FakeLLM(_refused()), "job_try": job_try}
        with pytest.raises(Retry) as again:
            await analyse_stage2(refused, "tenant-a", stage2_case.JOB)
        assert again.value.defer_score == delay * 1000
        job = await stage2_case._job()
        assert job.stage2 is Stage2State.PENDING
        assert job.passes.get("objections", 0) == 0

    last = {"llm": FakeLLM(_refused()), "job_try": stage2.STAGE2_TRIES}
    await analyse_stage2(last, "tenant-a", stage2_case.JOB)
    job = await stage2_case._job()
    assert (job.stage2, job.stage2_reason) == (Stage2State.FAILED, MODEL_REFUSED)


async def test_stage2_refused_then_answered_counts_one_start_per_pass(
    stage2_ctx: dict,
) -> None:
    await stage2_case._done_and_pending(stage2_ctx)
    with pytest.raises(Retry):
        await analyse_stage2(
            {"llm": FakeLLM(_refused()), "job_try": 1}, "tenant-a", stage2_case.JOB
        )
    await analyse_stage2(
        stage2_case._stage2_ctx(job_try=2), "tenant-a", stage2_case.JOB
    )
    job = await stage2_case._job()
    assert (job.stage2, job.passes) == (
        Stage2State.DONE,
        {"objections": 1, "escalations": 1, "coaching": 1, "extras": 1},
    )
