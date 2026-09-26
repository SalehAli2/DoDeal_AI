"""A lost reprompt (paid.py, M4): after a received malformed answer, a
reprompt that got no answer is sent once more ALONE -- the first answer read
back unpaid, the first prompt never sent again -- in a wave's pass, a
translation chunk and a WhatsApp suggestion alike. A run stopped during a
reprompt fails the pass rather than send the first prompt again."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.errors import ModelUnavailableError
from dodeal_ai.core.jobs import (
    Job,
    read_job,
    read_translation,
    read_work,
    start_pass,
    store_work,
)
from dodeal_ai.core.llm import LLMResponse
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_B_TRANSLATE
from dodeal_ai.core.prompting import AssembledPrompt
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.paid import (
    REPROMPTING,
    PassFailed,
    PassRun,
    PassUsage,
    run_pass,
)
from dodeal_ai.units.call_intelligence.prompts import (
    REPROMPT_TAIL_TEMPLATE,
    TRANSLATE_TEMPLATE,
    build_call_prompt,
)
from dodeal_ai.units.call_intelligence.translation import (
    TRANSLATE_LABEL,
    Translated,
    check_translation,
    chunks,
    translate_call,
    translate_data,
)
from dodeal_ai.units.call_intelligence.worker import job_scope
from dodeal_ai.units.structured_intelligence.llm_call import call_model
from tests.conftest import RedisFakes
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit import test_call_translation as translation_case
from tests.unit import test_call_whatsapp as whatsapp_case
from tests.unit.test_call_stage1 import SEGMENTS, _resolve

ARABIC = translation_case.ARABIC
GOOD = {"segments": [{"segment": f"s{n + 1}", "text": t} for n, t in enumerate(ARABIC)]}
# Ids not kept: a malformed answer, received and paid for, earning the reprompt.
BAD = {"segments": GOOD["segments"][:1]}
NAME = "extract"
CALL = CallText.of(translation_case.STORED, country_code="971")
(CHUNK,) = chunks(CALL)


def _tails(llm: FakeLLM) -> list[bool]:
    """Per call made, whether it was a reprompt (a tail) or the first prompt."""
    return [bool(call.prompt.tail) for call in llm.calls]


def _first(llm: FakeLLM) -> AssembledPrompt:
    return llm.calls[0].prompt


class _Peek:
    """The fake behind a check: each call first reads the job's work, so a test
    sees what was marked before the call was paid for."""

    def __init__(self, llm: FakeLLM, job: Job) -> None:
        self.llm, self.job = llm, job
        self.seen: list[dict[str, dict[str, object]]] = []

    async def complete(self, prompt: AssembledPrompt, **kwargs: Any) -> LLMResponse:
        self.seen.append(await read_work(self.job.tenant, self.job.job_id))
        return await self.llm.complete(prompt, **kwargs)


async def _job() -> Job:
    await translation_case._done_call(None)
    job = await read_job("tenant-a", translation_case.JOB)
    assert job is not None
    return job


async def _run(job: Job, client: Any, usage: PassUsage) -> Translated:
    work = await read_work(job.tenant, job.job_id)
    run = PassRun(job, work, 600, client, usage, start_pass)
    answer, _ = await run_pass(
        run,
        NAME,
        Translated,
        lambda metered: call_model(
            metered,
            build_call_prompt(TRANSLATE_TEMPLATE, translate_data(CALL, CHUNK, "ar")),
            Translated,
            TRANSLATE_LABEL,
            scope=job_scope(job),
            settings=get_settings(),
            profile=PROFILE_UNIT_B_TRANSLATE,
            max_output_tokens=2500,
            check=check_translation(CHUNK, "ar"),
            reprompt_tail=REPROMPT_TAIL_TEMPLATE,
        ),
    )
    return answer


# --- a wave's pass ----------------------------------------------------------------


async def test_a_lost_reprompt_is_sent_again_alone(redis_fakes: RedisFakes) -> None:
    """The guard: first prompt, reprompt lost, the reprompt once more; the
    first answer is read back, never paid for or counted again."""
    job = await _job()
    llm = FakeLLM(
        json_response(BAD, input_tokens=100, output_tokens=20),
        ModelUnavailableError(),
        json_response(GOOD, input_tokens=300, output_tokens=40),
    )
    peek = _Peek(llm, job)
    usage = PassUsage()
    answer = await _run(job, peek, usage)
    assert [line.text for line in answer.segments] == ARABIC
    assert _tails(llm) == [False, True, True]
    assert llm.calls[2].prompt == llm.calls[1].prompt != _first(llm)
    # Marked before each reprompt was paid for; nothing before the first prompt.
    assert [NAME in seen for seen in peek.seen] == [False, True, True]
    assert peek.seen[1][NAME] == {REPROMPTING: True}
    assert usage.tokens[NAME] == {"input": 400, "output": 60, "calls": 2}
    kept = await read_job(job.tenant, job.job_id)
    assert kept is not None and kept.passes[NAME] == 2
    work = await read_work(job.tenant, job.job_id)
    assert "answer" in work[NAME] and REPROMPTING not in work[NAME]


async def test_a_lost_first_prompt_is_the_one_sent_again(
    redis_fakes: RedisFakes,
) -> None:
    """No response arrived at all: the first prompt is sent once more."""
    job = await _job()
    llm = FakeLLM(ModelUnavailableError(), json_response(GOOD))
    await _run(job, llm, PassUsage())
    assert _tails(llm) == [False, False]


async def test_a_reprompt_lost_twice_fails_the_pass_with_two_starts(
    redis_fakes: RedisFakes,
) -> None:
    job = await _job()
    llm = FakeLLM(json_response(BAD), ModelUnavailableError(), ModelUnavailableError())
    with pytest.raises(PassFailed, match=f"^{NAME}_model_unavailable$"):
        await _run(job, llm, PassUsage())
    assert _tails(llm) == [False, True, True]
    assert (await read_work(job.tenant, job.job_id))[NAME] == {
        "failed": "model_unavailable"
    }


async def test_a_run_stopped_during_a_reprompt_never_resends_the_first_prompt(
    redis_fakes: RedisFakes,
) -> None:
    """The next run finds the mark: the first answer arrived, so the pass
    fails unpaid rather than ask the first prompt again."""
    job = await _job()
    await store_work(job.tenant, job.job_id, NAME, {REPROMPTING: True}, ttl_seconds=60)
    llm = FakeLLM()
    with pytest.raises(PassFailed, match=f"^{NAME}_pass_interrupted$"):
        await _run(job, llm, PassUsage())
    assert llm.call_count == 0
    assert (await read_work(job.tenant, job.job_id))[NAME] == {
        "failed": "pass_interrupted"
    }


# --- a translation chunk and a WhatsApp suggestion --------------------------------


async def test_a_translations_lost_reprompt_is_sent_again_alone(
    redis_fakes: RedisFakes,
) -> None:
    await translation_case._done_call(
        {"transcript": translation_case.STORED.model_dump(mode="json")}
    )
    llm = FakeLLM(json_response(BAD), ModelUnavailableError(), json_response(GOOD))
    async with httpx.AsyncClient() as http:
        ctx = {"llm": llm, "http": http, "resolve": _resolve}
        await translate_call(ctx, "tenant-a", translation_case.JOB, "ar")
    held = await read_translation("tenant-a", translation_case.JOB, "ar")
    assert held is not None and held["reason"] is None
    assert [s["text"] for s in held["segments"]] == ARABIC
    assert _tails(llm) == [False, True, True]


async def test_a_whatsapp_lost_reprompt_is_sent_again_alone(
    whatsapp_client: httpx.AsyncClient, redis_fakes: RedisFakes
) -> None:
    await whatsapp_case._done_call(whatsapp_case.RESULT)
    llm = whatsapp_case._answering(
        {"whatsapp": whatsapp_case.ENGLISH},
        ModelUnavailableError(),
        {"whatsapp": whatsapp_case.RUSSIAN},
    )
    answered = await whatsapp_case._post(whatsapp_client, "ru")
    assert answered.status_code == 200
    assert answered.json()["text"] == whatsapp_case.RUSSIAN
    assert _tails(llm) == [False, True, True]


whatsapp_client = whatsapp_case.client
assert len(SEGMENTS) == len(ARABIC)
