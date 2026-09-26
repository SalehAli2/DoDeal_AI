"""Stage 1 with wave 1 (worker.py, analysis.py): the transcript kept, the job
analysing, both passes, the payload, and the two guards -- a crash after wave 1
is kept re-pays nothing, and a model outage still delivers the transcript."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arq import Retry

from dodeal_ai.core.jobs import JobStatus, create_job, read_job, read_result, read_work
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import analysis, worker
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.alarms import alarm_list_digest
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.fake_transcriber import FakeTranscriber
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from dodeal_ai.units.call_intelligence.worker import STAGE1, process_call
from tests.helpers.fake_llm import FakeLLM, json_response

HOST = "audio.tenant-a.example"
JOB = "job-1"
ALARM = "text me on my own mobile"
ON = {
    "calls_enabled": True,
    "audio_hosts": [HOST],
    "number_detection_enabled": True,
    "alarm_phrases_enabled": True,
    "alarm_phrases": [ALARM],
}
# The company number the push names: not the one the agent gives on the call.
COMPANY_HASH = hashlib.sha256(b"971561112222").hexdigest()


def _say(start: float, speaker: str, text: str, confidence: float = 0.9) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="en",
        confidence=confidence,
    )


# An invented call: a budget, an agent's own number, and a viewing agreed.
SEGMENTS = (
    _say(0, "agent", "Good morning, this is the sales office about the villa."),
    _say(5, "lead", "I want a villa, my budget is 1,200,000 AED."),
    _say(10, "agent", "Text me on my own mobile 055 765 4321 for photos."),
    _say(15, "lead", "Shall we meet on Tuesday? I am happy with that."),
)


def _detail(value=None, state="not_mentioned", quote=None, segment=None) -> dict:
    return {"value": value, "state": state, "quote": quote, "segment": segment}


EXTRACTION: dict[str, Any] = {
    "wanted": {"text": "A villa.", "quote": "I want a villa", "segment": "s2"},
    "discussed": ["budget", "viewing"],
    "concerns": [],
    "agreed": [
        {
            "text": "a meeting on Tuesday",
            "quote": "Shall we meet on Tuesday?",
            "segment": "s4",
        }
    ],
    "next_step": {
        "action": "Meet",
        "owner": "agent",
        "due": "Tuesday",
        "quote": "Shall we meet on Tuesday?",
        "segment": "s4",
        "kind": "office_visit",
    },
    "ending": "moved_forward",
    "details": {
        "budget": _detail(
            "1,200,000 AED", "stated", "my budget is 1,200,000 AED", "s2"
        ),
        **{
            name: _detail()
            for name in (
                "area",
                "property_reference",
                "timeline",
                "payment_method",
                "decision_maker",
            )
        },
    },
    "mood": {"value": "positive", "quote": "I am happy with that", "segment": "s4"},
}
PROSE = {
    "summary": "The client wants a villa. They meet on Tuesday.",
    "crm_note": "Villa, budget 1,200,000 AED; meeting Tuesday.",
}


class _Deliveries:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __call__(self, job, event: str, config) -> bool:
        self.events.append(event)
        return True


async def _resolve(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _audio(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=b"RIFF" + b"\x00" * 64, headers={"content-type": "audio/wav"}
    )


def _good_llm() -> FakeLLM:
    return FakeLLM(json_response(EXTRACTION), json_response(PROSE))


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(_audio)) as http:
        yield {
            "http": http,
            "transcriber": FakeTranscriber(SEGMENTS),
            "resolve": _resolve,
            "deliver": _Deliveries(),
            "llm": _good_llm(),
        }


async def _push(
    duration: int = 150, lead_hash: str | None = None, **config: object
) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {**ON, **config},
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )
    body = CallJobRequest.model_validate(
        {
            "call_id": 7,
            "lead_id": 1656,
            "author_id": 27,
            "duration_seconds": duration,
            "recorded_at": "2026-09-23T08:00:00+00:00",
            "audio_url": f"https://{HOST}/calls/7.wav?sig=SIGNED",
            "audio_url_expires_at": (
                datetime.now(UTC) + timedelta(hours=2)
            ).isoformat(),
            "agent_phone_hash": COMPANY_HASH,
            "lead_phone_hash": lead_hash,
        }
    )
    await create_job(
        "tenant-a",
        7,
        job_id=JOB,
        request_id="req-push",
        queue=NORMAL_QUEUE,
        metadata=job_metadata(body, parse_unit_b_section(ON)),
        now=datetime.now(UTC),
    )


async def _result() -> dict[str, Any]:
    result = await read_result("tenant-a", JOB)
    assert result is not None
    return result


async def _status() -> JobStatus:
    job = await read_job("tenant-a", JOB)
    assert job is not None
    return job.status


# --- the payload -----------------------------------------------------------------


async def test_a_call_is_analysed_and_delivered_with_its_stage1_payload(
    ctx: dict,
) -> None:
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    assert await _status() is JobStatus.DONE
    assert ctx["deliver"].events == [STAGE1]
    assert result["transcript"]["segments"][2]["text"] == SEGMENTS[2].text
    assert result["analysis_reason"] is None
    analysis_ = result["analysis"]
    assert set(analysis_) == {
        "language",
        "uncertain",
        "summary",
        "elements",
        "details",
        "mood",
        "crm_note",
    }
    assert (analysis_["language"], analysis_["summary"]) == ("en", PROSE["summary"])
    assert analysis_["elements"]["ending"] == "moved_forward"
    assert analysis_["elements"]["loss_reason"] is None
    assert analysis_["elements"]["agreed"] == [
        {**EXTRACTION["agreed"][0], "unverified": False}
    ]
    assert analysis_["elements"]["next_step"]["segment"] == "s4"
    step = analysis_["elements"]["next_step"]
    assert (step["kind"], step["when"], step["when_state"], step["booked"]) == (
        "office_visit",
        None,
        "uncertain",
        False,
    )
    assert analysis_["details"]["budget"]["state"] == "stated"
    assert analysis_["details"]["area"] == {**_detail(), "evidence_failed": False}
    assert analysis_["mood"]["uncertain"] is False
    assert analysis_["mood"]["evidence_failed"] is False
    signals = result["signals"]
    assert set(signals) == {
        "version",
        "agent",
        "client",
        "talk_balance",
        "talk_reason",
        "numbers",
        "alarms",
        "keywords",
        "escalations",
    }
    assert signals["keywords"] is None
    assert signals["numbers"] == [
        {
            "speaker": "agent",
            "start_s": 10.0,
            "segment": "s3",
            "last4": "4321",
            "match": "agent_personal",
        }
    ]
    assert [(e["source"], e["segment"]) for e in signals["escalations"]] == [
        ("number", "s3"),
        ("alarm_phrase", "s3"),
    ]
    assert result["versions"] == {
        "prompt": "unit_b_prompts_v11",
        "signals": "call_signals_v2",
        "model": "fake-model-pinned",
        "transcriber": "fake/fake-stt-1",
        "alarm_list_digest": alarm_list_digest([ALARM]),
        "passes": {
            name: {"provider": None, "model": "fake-model-pinned"}
            for name in ("extract", "prose")
        },
    }
    assert await read_work("tenant-a", JOB) == {}


async def test_each_pass_hands_the_adapter_its_own_schema(ctx: dict) -> None:
    """For a json_schema profile, register item 147; unused under json_object."""
    from dodeal_ai.units.call_intelligence.passes import Extraction, Prose

    await _push()
    await process_call(ctx, "tenant-a", JOB)
    schemas = [call.response_schema for call in ctx["llm"].calls]
    assert schemas == [Extraction.model_json_schema(), Prose.model_json_schema()]


async def test_the_model_never_reads_the_agents_number(ctx: dict) -> None:
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    for prompt in ctx["llm"].prompts:
        assert "765 4321" not in prompt.text
        assert "Text me on my own mobile [PHONE] for photos." in prompt.text


async def test_the_tenants_country_code_reaches_the_number_match(ctx: dict) -> None:
    """A Saudi tenant's lead number, said locally, matches its 966 hash."""
    ctx["transcriber"] = FakeTranscriber(
        (_say(0, "lead", "Call me back on 050 123 4567 please."),)
    )
    saudi = hashlib.sha256(b"966501234567").hexdigest()
    await _push(lead_hash=saudi, phone_country_code="966")
    await process_call(ctx, "tenant-a", JOB)

    (find,) = (await _result())["signals"]["numbers"]
    assert (find["match"], find["last4"]) == ("lead", "4567")


async def test_the_switches_off_find_no_numbers_or_phrases(ctx: dict) -> None:
    await _push(number_detection_enabled=False, alarm_phrases_enabled=False)
    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    signals = result["signals"]
    assert (signals["numbers"], signals["alarms"], signals["escalations"]) == (
        None,
        None,
        [],
    )
    assert result["versions"]["alarm_list_digest"] is None


async def test_a_call_under_two_minutes_gets_wave_1_too(ctx: dict) -> None:
    await _push(duration=60)
    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    assert result["eligible_for_full_analysis"] is False
    assert result["analysis"] is not None and ctx["llm"].call_count == 2


async def test_an_uncertain_transcript_gets_wave_1_with_every_field_uncertain(
    ctx: dict,
) -> None:
    ctx["transcriber"] = FakeTranscriber(
        tuple(_say(s.start_s, s.speaker, s.text, 0.3) for s in SEGMENTS)
    )
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    analysis_ = (await _result())["analysis"]
    assert analysis_["uncertain"] is True
    assert {d["state"] for d in analysis_["details"].values()} == {"uncertain"}
    assert analysis_["details"]["area"]["value"] is None
    assert analysis_["mood"]["uncertain"] is True


# --- the guards --------------------------------------------------------------------


async def test_a_crash_after_wave_1_is_kept_re_pays_nothing(
    ctx: dict, monkeypatch
) -> None:
    await _push()
    real_store = worker.store_result
    crashes = [RuntimeError("the worker died storing stage 1")]

    async def store_once(*args: Any, **kwargs: Any) -> None:
        if crashes:
            raise crashes.pop()
        await real_store(*args, **kwargs)

    monkeypatch.setattr(worker, "store_result", store_once)
    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    assert await _status() is JobStatus.ANALYSING
    assert set(await read_work("tenant-a", JOB)) == {"transcript", "extract", "prose"}

    await process_call(ctx, "tenant-a", JOB)

    assert ctx["llm"].call_count == 2
    assert len(ctx["transcriber"].calls) == 1
    assert await _status() is JobStatus.DONE
    assert (await _result())["analysis"]["crm_note"] == PROSE["crm_note"]
    assert ctx["deliver"].events == [STAGE1]


async def test_a_model_outage_delivers_the_transcript_with_analysis_null(
    ctx: dict,
) -> None:
    """The response never arrived: started once more, then the pass fails."""
    ctx["llm"] = FakeLLM(RuntimeError("provider down"), RuntimeError("still down"))
    await _push()

    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    assert (result["analysis"], result["analysis_reason"]) == (
        None,
        "extract_model_unavailable",
    )
    assert result["transcript"] is not None and result["versions"]["model"] is None
    assert ctx["llm"].call_count == 2
    assert await _status() is JobStatus.DONE
    assert ctx["deliver"].events == [STAGE1]


@pytest.mark.parametrize("engine_doubts", [(), ("stt_no_speakers",)])
async def test_a_transcript_with_no_segments_is_no_speech_and_calls_no_model(
    ctx: dict[str, Any], engine_doubts: tuple[str, ...]
) -> None:
    """The guard (F-5): nothing heard, so wave 1 is not run -- no roles, no
    pass, no model call -- and stage 1 goes with analysis null, no_speech."""

    class _Silent(FakeTranscriber):
        async def transcribe(
            self,
            audio_path: Path,
            *,
            language_hint: str | None,
            duration_seconds: float,
        ) -> Transcript:
            await super().transcribe(
                audio_path,
                language_hint=language_hint,
                duration_seconds=duration_seconds,
            )
            return Transcript.of(
                (), provider="recorded", model="recorded-stt-1", reasons=engine_doubts
            )

    ctx["transcriber"] = _Silent(())
    ctx["llm"] = FakeLLM()
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    assert ctx["llm"].calls == []
    result = await _result()
    assert (result["analysis"], result["analysis_reason"]) == (None, "no_speech")
    transcript = result["transcript"]
    assert transcript["segments"] == [] and transcript["uncertain"] is True
    assert transcript["uncertain_reasons"] == [*engine_doubts, "no_speech"]
    assert (result["roles"], result["signals"], result["versions"]) == (
        None,
        None,
        None,
    )
    assert result["eligible_for_full_analysis"] is False
    assert ctx["deliver"].events == ["call.stage1"]
    assert await _status() is JobStatus.DONE


@pytest.mark.parametrize("llm", ["down", "none"])
async def test_a_model_outage_still_delivers_the_off_channel_escalation(
    ctx: dict, llm: str
) -> None:
    """The signals block goes out top-level whatever becomes of the passes."""
    ctx["llm"] = (
        FakeLLM(RuntimeError("provider down"), RuntimeError("still down"))
        if llm == "down"
        else None
    )
    await _push()

    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    assert result["analysis"] is None
    assert result["signals"]["client"]["interruptions"] == 0
    assert [(e["type"], e["source"]) for e in result["signals"]["escalations"]] == [
        ("off_channel_contact", "number"),
        ("off_channel_contact", "alarm_phrase"),
    ]
    assert ctx["deliver"].events == [STAGE1]


# --- a pass that fails -------------------------------------------------------------


async def test_a_malformed_extraction_twice_fails_the_pass_without_the_prose(
    ctx: dict,
) -> None:
    ctx["llm"] = FakeLLM(json_response({"wanted": None}), json_response({}))
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    assert result["analysis_reason"] == "extract_malformed_output"
    assert ctx["llm"].call_count == 2


async def test_a_prose_outage_fails_stage_1_analysis_with_the_extract_model(
    ctx: dict,
) -> None:
    ctx["llm"] = FakeLLM(
        json_response(EXTRACTION), RuntimeError("down"), RuntimeError("down")
    )
    await _push()
    await process_call(ctx, "tenant-a", JOB)

    result = await _result()
    assert result["analysis_reason"] == "prose_model_unavailable"
    assert result["versions"]["model"] == "fake-model-pinned"
    assert ctx["llm"].call_count == 3


async def test_a_failed_pass_kept_before_a_crash_is_not_paid_again(
    ctx: dict, monkeypatch
) -> None:
    ctx["llm"] = FakeLLM(json_response({}), json_response({}))
    await _push()
    real_store = worker.store_result
    crashes = [RuntimeError("the worker died")]

    async def store_once(*args: Any, **kwargs: Any) -> None:
        if crashes:
            raise crashes.pop()
        await real_store(*args, **kwargs)

    monkeypatch.setattr(worker, "store_result", store_once)
    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    await process_call(ctx, "tenant-a", JOB)

    assert (await _result())["analysis_reason"] == "extract_malformed_output"
    assert ctx["llm"].call_count == 2


class _DyingPass:
    """Counts its start, then dies the way a killed worker does mid-call."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, *args: Any, **kwargs: Any):
        self.calls += 1
        raise asyncio.CancelledError


@pytest.mark.parametrize(
    ("deaths", "reason", "calls"),
    [(1, None, 2), (2, "extract_pass_interrupted", 0)],
    ids=["started-once-more", "interrupted-twice"],
)
async def test_a_pass_cut_off_mid_call_is_started_once_more_at_most(
    ctx: dict, monkeypatch, deaths: int, reason: str | None, calls: int
) -> None:
    await _push()
    dying = _DyingPass()
    with monkeypatch.context() as patched:
        patched.setattr(analysis, "extract", dying)
        for _ in range(deaths):
            with pytest.raises(asyncio.CancelledError):
                await process_call(ctx, "tenant-a", JOB)
    job = await read_job("tenant-a", JOB)
    assert job is not None and job.passes == {"extract": deaths}

    await process_call(ctx, "tenant-a", JOB)

    assert (await _result())["analysis_reason"] == reason
    assert ctx["llm"].call_count == calls
    assert len(ctx["transcriber"].calls) == 1


async def test_a_deadline_during_analysis_re_runs_and_resumes(
    ctx: dict, monkeypatch
) -> None:
    await _push()
    ctx["llm"] = FakeLLM(hold_after=0)
    monkeypatch.setattr(worker, "deadline_seconds", lambda: 0.5)
    with pytest.raises(Retry):
        await asyncio.wait_for(process_call(ctx, "tenant-a", JOB), 5)
    assert await _status() is JobStatus.ANALYSING

    ctx["llm"] = _good_llm()
    await process_call(ctx, "tenant-a", JOB)

    assert (await _result())["analysis_reason"] is None
    job = await read_job("tenant-a", JOB)
    assert job is not None and job.passes == {"extract": 2, "prose": 1}


# --- the job going away under wave 1 --------------------------------------------------


async def test_a_job_gone_before_a_pass_starts_stores_nothing(
    ctx: dict, monkeypatch
) -> None:
    await _push()

    async def gone(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(analysis, "start_pass", gone)
    await process_call(ctx, "tenant-a", JOB)
    assert await read_result("tenant-a", JOB) is None
    assert ctx["deliver"].events == []

    await process_call(ctx, "tenant-a", JOB)
    assert await read_result("tenant-a", JOB) is None


async def test_a_job_failed_during_transcription_is_not_analysed(
    ctx: dict, redis_fakes
) -> None:
    await _push()

    class _Failing(FakeTranscriber):
        async def transcribe(
            self,
            audio_path: Path,
            *,
            language_hint: str | None,
            duration_seconds: float,
        ):
            await redis_fakes.jobs.hset(f"call_job:tenant-a:{JOB}", "status", "failed")
            return await super().transcribe(
                audio_path,
                language_hint=language_hint,
                duration_seconds=duration_seconds,
            )

    ctx["transcriber"] = _Failing(SEGMENTS)
    await process_call(ctx, "tenant-a", JOB)
    assert await read_result("tenant-a", JOB) is None
    assert ctx["llm"].call_count == 0


# --- the outcome line ------------------------------------------------------------------


async def test_the_outcome_line_carries_each_passes_tokens(
    ctx: dict, caplog: pytest.LogCaptureFixture
) -> None:
    await _push()
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await process_call(ctx, "tenant-a", JOB)

    (outcome,) = [r for r in caplog.records if r.getMessage() == "call_job_outcome"]
    spent = {"input": 100, "output": 20, "calls": 1}
    assert outcome.pass_tokens == {"extract": spent, "prose": spent}
    assert isinstance(outcome.analyse_ms, int) and outcome.analysis_reason is None
    assert "1,200,000" not in json.dumps(outcome.__dict__, default=str)


async def test_the_extract_pass_reads_the_calls_time_in_the_tenants_zone(
    ctx: dict,
) -> None:
    """The push's recorded_at (08:00 UTC) and the tenant's zone reach the data
    half, and a next step's time is delivered in that zone."""
    booked = copy.deepcopy(EXTRACTION)
    booked["next_step"].update(when="2026-09-24T14:00:00Z", booked=True)
    ctx["llm"] = FakeLLM(json_response(booked), json_response(PROSE))
    await _push(timezone="Asia/Riyadh")

    await process_call(ctx, "tenant-a", JOB)

    extract_data = ctx["llm"].calls[0].prompt.variable
    assert "RECORDED AT: 2026-09-23T11:00:00+03:00\nTIMEZONE: Asia/Riyadh" in (
        extract_data
    )
    step = (await _result())["analysis"]["elements"]["next_step"]
    assert (step["when"], step["when_state"], step["booked"]) == (
        "2026-09-24T17:00:00+03:00",
        "stated",
        True,
    )
