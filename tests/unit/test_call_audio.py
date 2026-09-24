"""The audio layer (unit-b audio): inspected before any paid call, stereo split
into two sides and merged as agent and client, poor audio uncertain whatever
the engine says, and a call over 3600 s a permanent failure. ffmpeg is a fake
runner answering with recorded output (tests/fixtures/audio)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from arq import Retry

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.jobs import JobStatus, create_job, read_job, read_result, read_work
from dodeal_ai.core.tenant_config import set_override
from dodeal_ai.units.call_intelligence import audio
from dodeal_ai.units.call_intelligence.admission import job_metadata
from dodeal_ai.units.call_intelligence.audio import (
    AudioError,
    AudioQuality,
    AudioTools,
    FfmpegMissing,
    ensure_ffmpeg,
    merge_sides,
    parse_quality,
    run_ffmpeg,
    run_ffprobe,
)
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.http_stt import DiarizedHttpTranscriber
from dodeal_ai.units.call_intelligence.queues import NORMAL_QUEUE, STAGE2_QUEUE
from dodeal_ai.units.call_intelligence.schemas import CallJobRequest
from dodeal_ai.units.call_intelligence.transcriber import (
    Segment,
    Transcript,
    TranscriptionError,
)
from dodeal_ai.units.call_intelligence.worker import process_call
from dodeal_ai.workers import calls as calls_worker
from tests.conftest import RedisFakes

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "audio"
HOST = "audio.tenant-a.example"
JOB = "job-1"
ON = {"calls_enabled": True, "audio_hosts": [HOST]}
LEFT, RIGHT = b"LEFT" * 64, b"RIGHT" * 64
FLAC = b"fLaC" + b"\x00" * 60
M4A = b"\x00\x00\x00\x20ftypM4A " + b"\x00" * 60
STT_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stt"


def _recorded(name: str) -> str:
    return (FIXTURES / f"{name}_inspect.txt").read_text(encoding="utf-8")


def _segment(
    start: float, end: float, text: str, speaker: str = "speaker_1"
) -> Segment:
    return Segment(
        start_s=start,
        end_s=end,
        speaker=speaker,
        text=text,
        language="en",
        confidence=0.9,
    )


class _Ffmpeg:
    """A recorded ffmpeg and ffprobe: every run's arguments, the inspect output
    named, a split that writes one file per side, a conversion that writes
    one, and a probe that decodes `frames` frames."""

    def __init__(
        self, inspect: str, *, split_code: int = 0, frames: str = "1\n"
    ) -> None:
        self.inspect = inspect
        self.split_code = split_code
        self.frames = frames
        self.runs: list[list[str]] = []
        self.probes: list[list[str]] = []

    async def __call__(self, args: Sequence[str]) -> tuple[int, str]:
        self.runs.append(list(args))
        outputs = [Path(args[i + 2]) for i, a in enumerate(args) if a == "-ar"]
        if "-filter_complex" in args:
            for path, body in zip(outputs, (LEFT, RIGHT), strict=True):
                path.write_bytes(body)
            return self.split_code, ""
        if outputs:
            (flac,) = outputs
            flac.write_bytes(FLAC)
            return 0, ""
        return 0, _recorded(self.inspect)

    async def probe(self, args: Sequence[str]) -> tuple[int, str]:
        self.probes.append(list(args))
        return 0, self.frames


def _tools(ffmpeg: _Ffmpeg) -> AudioTools:
    return AudioTools(ffmpeg, ffmpeg.probe)


class _Sides:
    """A transcriber that answers by which side's file it was handed."""

    def __init__(self, *, fail_right: TranscriptionError | None = None) -> None:
        self.heard: list[bytes] = []
        self.fail_right = fail_right

    async def transcribe(
        self, audio_path: Path, *, language_hint: str | None
    ) -> Transcript:
        body = audio_path.read_bytes()
        self.heard.append(body)
        if body == LEFT:
            segments = (
                _segment(0.0, 4.0, "Good morning, this is the sales office."),
                _segment(9.0, 12.0, "Shall we book Tuesday?"),
            )
        elif body == RIGHT:
            if self.fail_right is not None:
                raise self.fail_right
            segments = (
                _segment(3.5, 8.5, "Hello, yes, I am looking for a villa."),
                _segment(12.5, 14.0, "Tuesday works."),
            )
        else:
            segments = (_segment(0.0, 5.0, "One mixed track."),)
        return Transcript.of(segments, provider="recorded", model="recorded-stt-1")


class _Deliveries:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def __call__(self, job, event: str, config) -> bool:
        self.events.append(event)
        return True


async def _resolve(host: str, port: int) -> list[str]:
    return ["93.184.216.34"]


def _audio_file() -> httpx.Response:
    return httpx.Response(
        200, content=b"RIFF" + b"\x00" * 256, headers={"content-type": "audio/wav"}
    )


@pytest.fixture
async def ctx() -> AsyncIterator[dict[str, Any]]:
    transport = httpx.MockTransport(lambda request: _audio_file())
    async with httpx.AsyncClient(transport=transport) as http:
        yield {
            "http": http,
            "transcriber": _Sides(),
            "resolve": _resolve,
            "deliver": _Deliveries(),
            "audio": _tools(_Ffmpeg("mono")),
        }


async def _calls_on(**changes: object) -> None:
    await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {**ON, **changes},
        now=datetime.now(UTC),
        keep_version=new_config_version,
    )


async def _push(duration: int = 150) -> None:
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


# --- the guards --------------------------------------------------------------


async def test_a_stereo_recording_splits_into_two_sides_merged_as_agent_and_client(
    ctx: dict[str, Any],
) -> None:
    """Two 16 kHz mono files, each transcribed once, merged by time; the
    agent's side is the one the setting names."""
    ffmpeg = _Ffmpeg("stereo")
    ctx["audio"] = _tools(ffmpeg)
    await _calls_on(audio_channels="stereo_agent_left")
    await _push()

    await process_call(ctx, "tenant-a", JOB)

    (_, split) = ffmpeg.runs
    assert split.count("-ar") == 2 and split.count("16000") == 2
    assert split.count("flac") == 2
    assert "[0:a]pan=mono|c0=c0[l];[0:a]pan=mono|c0=c1[r]" in split
    assert sorted(ctx["transcriber"].heard) == sorted([LEFT, RIGHT])
    result = await read_result("tenant-a", JOB)
    assert result is not None
    segments = result["transcript"]["segments"]
    assert [(s["speaker"], s["start_s"], s["end_s"]) for s in segments] == [
        ("agent", 0.0, 4.0),
        ("client", 4.0, 8.5),
        ("agent", 9.0, 12.0),
        ("client", 12.5, 14.0),
    ]
    assert result["audio"] == {
        "duration_seconds": 150.0,
        "channels": 2,
        "speech_ratio": 0.917,
        "mean_volume_db": -22.4,
    }
    assert result["transcript"]["uncertain"] is False
    sides = [Path(split[i + 2]) for i, a in enumerate(split) if a == "-ar"]
    assert [path.suffix for path in sides] == [".flac", ".flac"]
    assert not any(path.parent.exists() for path in sides)


async def test_a_near_silent_recording_is_uncertain_whatever_the_engine_says(
    ctx: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    ctx["audio"] = _tools(_Ffmpeg("near_silent"))
    await _calls_on()
    await _push()

    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await process_call(ctx, "tenant-a", JOB)

    result = await read_result("tenant-a", JOB)
    assert result is not None
    transcript = result["transcript"]
    assert transcript["uncertain"] is True
    # The engine's label with no roles answer adds its own doubt (roles.py).
    assert transcript["uncertain_reasons"] == [
        "low_speech_ratio",
        "low_volume",
        "roles_failed",
    ]
    assert result["eligible_for_full_analysis"] is False
    assert result["audio"]["speech_ratio"] == 0.2
    assert result["audio"]["mean_volume_db"] == -55.8
    (line,) = [r for r in caplog.records if r.getMessage() == "call_job_outcome"]
    assert (line.speech_ratio, line.mean_volume_db) == (0.2, -55.8)


async def test_a_file_over_3600_seconds_fails_permanently_unpaid(
    ctx: dict[str, Any], redis_fakes: RedisFakes
) -> None:
    ctx["audio"] = _tools(_Ffmpeg("too_long"))
    await _calls_on()
    await _push()

    await process_call(ctx, "tenant-a", JOB)

    job = await read_job("tenant-a", JOB)
    assert job is not None
    assert (job.status, job.reason) == (JobStatus.FAILED, "audio_too_long")
    assert job.transcriptions == 0 and ctx["transcriber"].heard == []
    assert ctx["deliver"].events == ["call.failed"]
    assert not [k for k in redis_fakes.cost.store if "audio_seconds" in k]


# --- refused before any paid call ----------------------------------------------


@pytest.mark.parametrize(
    ("changes", "inspect", "duration", "reason"),
    [
        ({}, "mono", 3601, "audio_too_long"),
        (
            {"audio_channels": "stereo_agent_right"},
            "mono",
            150,
            "audio_channels_mismatch",
        ),
        ({}, "unreadable", 150, "audio_unreadable"),
    ],
)
async def test_each_refusal_is_permanent_and_unpaid(
    ctx: dict[str, Any], changes: dict, inspect: str, duration: int, reason: str
) -> None:
    ffmpeg = _Ffmpeg(inspect)
    if inspect == "unreadable":
        ffmpeg = _Ffmpeg("mono")
        ffmpeg.inspect = "mono"

        async def _broken(args: Sequence[str]) -> tuple[int, str]:
            return 1, "call.wav: Invalid data found when processing input"

        ctx["audio"] = AudioTools(_broken, ffmpeg.probe)
    else:
        ctx["audio"] = _tools(ffmpeg)
    await _calls_on(**changes)
    await _push(duration)

    await process_call(ctx, "tenant-a", JOB)

    job = await read_job("tenant-a", JOB)
    assert job is not None and (job.status, job.reason) == (JobStatus.FAILED, reason)
    assert ctx["transcriber"].heard == []


async def test_stereo_without_ffmpeg_fails_before_the_download(
    ctx: dict[str, Any],
) -> None:
    del ctx["audio"]
    await _calls_on(audio_channels="stereo_agent_left")
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    job = await read_job("tenant-a", JOB)
    assert job is not None and job.reason == "audio_tools_unavailable"


async def test_mono_without_ffmpeg_is_transcribed_with_its_numbers_null(
    ctx: dict[str, Any],
) -> None:
    """The demo's case: no tools, no numbers, the one track sent as it came."""
    del ctx["audio"]
    await _calls_on()
    await _push()
    await process_call(ctx, "tenant-a", JOB)
    result = await read_result("tenant-a", JOB)
    assert result is not None and result["audio"] is None
    assert len(ctx["transcriber"].heard) == 1


async def test_a_side_received_is_never_paid_for_again(
    ctx: dict[str, Any], redis_fakes: RedisFakes
) -> None:
    """The right side fails and may pass later: the retry pays for it alone."""
    ctx["audio"] = _tools(_Ffmpeg("stereo"))
    ctx["transcriber"] = _Sides(
        fail_right=TranscriptionError("stt_unavailable", retryable=True)
    )
    await _calls_on(audio_channels="stereo_agent_left")
    await _push()

    with pytest.raises(Retry):
        await process_call(ctx, "tenant-a", JOB)
    assert set(await read_work("tenant-a", JOB)) == {"audio", "transcript:agent"}
    assert ctx["transcriber"].heard == [LEFT, RIGHT]

    ctx["transcriber"].fail_right = None
    await process_call(ctx, "tenant-a", JOB)
    assert ctx["transcriber"].heard == [LEFT, RIGHT, RIGHT]
    job = await read_job("tenant-a", JOB)
    assert job is not None and job.status is JobStatus.DONE
    # Stereo is charged for both sides, on each attempt, as a retry always is.
    charged = [v for k, v in redis_fakes.cost.store.items() if "audio_seconds" in k]
    assert [int(value) for value in charged] == [2 * 2 * 150]


async def test_an_m4a_is_converted_and_the_engine_is_sent_flac(
    ctx: dict[str, Any],
) -> None:
    """The guard (F-1): whatever came down, ffprobe decoded it, ffmpeg made it
    16 kHz mono FLAC, and the engine is posted that file as audio/flac."""
    engine: list[httpx.Request] = []

    def _engine(request: httpx.Request) -> httpx.Response:
        engine.append(request)
        body = (STT_FIXTURES / "diarized_http.json").read_text(encoding="utf-8")
        return httpx.Response(200, content=body.encode("utf-8"))

    def _m4a(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=M4A, headers={"content-type": "audio/mp4"})

    ffmpeg = _Ffmpeg("mono")
    ctx["audio"] = _tools(ffmpeg)
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(_m4a)) as download,
        httpx.AsyncClient(transport=httpx.MockTransport(_engine)) as stt,
    ):
        ctx["http"] = download
        ctx["transcriber"] = DiarizedHttpTranscriber(
            base_url="http://stt.test",
            model="m1",
            api_key="test-key",
            http=stt,
            timeout_seconds=30,
        )
        await _calls_on()
        await _push()
        await process_call(ctx, "tenant-a", JOB)

    (probe,) = ffmpeg.probes
    assert probe[probe.index("-select_streams") + 1] == "a:0"
    assert "-count_frames" in probe
    (_, convert) = ffmpeg.runs
    downloaded = convert[convert.index("-i") + 1]
    assert probe[-1] == downloaded
    assert convert[convert.index("-map") :] == [
        *("-map", "0:a:0", "-ac", "1", "-c:a", "flac", "-ar", "16000"),
        convert[-1],
    ]
    assert Path(convert[-1]).suffix == ".flac"
    assert not Path(convert[-1]).parent.exists()
    (request,) = engine
    form = request.content
    assert b'filename="call.flac"\r\nContent-Type: audio/flac\r\n\r\n' + FLAC in form
    assert M4A not in form
    job = await read_job("tenant-a", JOB)
    assert job is not None and job.status is JobStatus.DONE


@pytest.mark.parametrize(
    ("code", "frames"), [(0, ""), (0, "0\n"), (0, "N/A\n"), (1, "1\n")]
)
async def test_a_file_ffprobe_cannot_decode_is_a_permanent_unpaid_failure(
    ctx: dict[str, Any], redis_fakes: RedisFakes, code: int, frames: str
) -> None:
    """No frame decoded, or ffprobe failing: audio_format_unknown, before
    ffmpeg, the charge or any engine."""
    ffmpeg = _Ffmpeg("mono", frames=frames)

    async def _probe(args: Sequence[str]) -> tuple[int, str]:
        return code, frames

    ctx["audio"] = AudioTools(ffmpeg, _probe)
    await _calls_on()
    await _push()

    await process_call(ctx, "tenant-a", JOB)

    job = await read_job("tenant-a", JOB)
    assert job is not None
    assert (job.status, job.reason) == (JobStatus.FAILED, "audio_format_unknown")
    assert ffmpeg.runs == [] and ctx["transcriber"].heard == []
    assert job.transcriptions == 0 and ctx["deliver"].events == ["call.failed"]
    assert not [k for k in redis_fakes.cost.store if "audio_seconds" in k]


async def test_a_resumed_run_keeps_the_audio_numbers(ctx: dict[str, Any]) -> None:
    """A run that finds the transcript kept reports the numbers it was kept with."""
    ctx["audio"] = _tools(_Ffmpeg("mono"))
    ctx["deliver"] = _Crashing()
    await _calls_on()
    await _push()
    ctx["llm"] = _Down()
    with pytest.raises(RuntimeError):
        await process_call(ctx, "tenant-a", JOB)
    ctx["llm"] = None
    ctx["deliver"] = _Deliveries()
    await process_call(ctx, "tenant-a", JOB)
    result = await read_result("tenant-a", JOB)
    assert result is not None and result["audio"]["mean_volume_db"] == -24.0


class _Crashing(_Deliveries):
    async def __call__(self, job, event: str, config) -> bool:
        raise RuntimeError("the worker died")


class _Down:
    """A model client whose first call kills the run, as a crash would."""

    async def complete(
        self, prompt, *, profile, max_output_tokens=None, response_schema=None
    ):
        raise RuntimeError("the worker died mid-pass")


# --- the parts ---------------------------------------------------------------


def test_the_recorded_numbers_parse() -> None:
    stereo = parse_quality(_recorded("stereo"))
    assert stereo == AudioQuality(150.0, 2, 0.917, -22.4) and stereo.reasons() == ()
    silent = parse_quality(_recorded("near_silent"))
    assert (silent.channels, silent.speech_ratio) == (1, 0.2)
    assert (
        parse_quality(
            "Duration: 00:00:10.00\n  Audio: aac, 44100 Hz, 6 channels"
        ).channels
        == 6
    )
    unknown = parse_quality("Duration: 00:00:00.00")
    assert (unknown.channels, unknown.speech_ratio, unknown.mean_volume_db) == (
        None,
        None,
        None,
    )
    assert unknown.reasons() == ("low_speech_ratio", "low_volume")
    with pytest.raises(AudioError, match="^audio_unreadable$"):
        parse_quality("call.wav: No such file or directory")
    assert AudioQuality.from_dict(stereo.to_dict()) == stereo
    assert AudioQuality.from_dict(unknown.to_dict()) == unknown


async def test_a_failed_split_is_unreadable_and_leaves_no_directory(
    tmp_path: Path,
) -> None:
    ffmpeg = _Ffmpeg("stereo", split_code=1)
    tools = _tools(ffmpeg)
    with pytest.raises(AudioError, match="^audio_unreadable$"):
        async with tools.split(tmp_path / "call.wav"):
            raise AssertionError("never reached")
    (split,) = ffmpeg.runs
    assert not Path(split[-1]).parent.exists()


def test_the_sides_merge_with_the_agent_on_the_side_named() -> None:
    left = Transcript.of((_segment(0.0, 5.0, "left words"),), provider="p", model="m")
    right = Transcript.of(
        (_segment(1.0, 3.0, "inside the left"),), provider="p", model="m"
    )
    roles = audio.side_roles("stereo_agent_right")
    merged = merge_sides(
        list(zip(roles, (left, right), strict=True)), provider="p", model="m"
    )
    assert [(s.speaker, s.start_s, s.end_s) for s in merged.segments] == [
        ("client", 0.0, 5.0),
        ("agent", 5.0, 5.0),
    ]


async def test_ffmpeg_is_checked_and_a_missing_one_named() -> None:
    async def _fails(args: Sequence[str]) -> tuple[int, str]:
        return 1, ""

    async def _works(args: Sequence[str]) -> tuple[int, str]:
        return 0, "ffmpeg version 7.1"

    with pytest.raises(FfmpegMissing, match="^ffmpeg_not_found$"):
        await ensure_ffmpeg()
    with pytest.raises(FfmpegMissing):
        await ensure_ffmpeg(_fails, _works)
    with pytest.raises(FfmpegMissing):
        await ensure_ffmpeg(_works, _fails)
    with pytest.raises(FfmpegMissing):
        await ensure_ffmpeg(_works)
    assert isinstance(await ensure_ffmpeg(_works, _works), AudioTools)


class _Process:
    """An asyncio subprocess stand-in: answers, or hangs until killed."""

    def __init__(self, *, hang: bool) -> None:
        self.hang, self.killed = hang, False
        self.returncode: int | None = None

    async def communicate(self) -> tuple[bytes, bytes]:
        if self.hang:
            await asyncio.Event().wait()
        self.returncode = 0
        return b"1\n", b"mean_volume: -20.0 dB\n"

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.returncode = -9
        return -9


async def test_run_ffmpeg_reads_stderr_and_kills_a_cancelled_run(monkeypatch) -> None:
    made: list[tuple[tuple, _Process]] = []

    async def _spawn(*args: object, **kwargs: object) -> _Process:
        process = _Process(hang=len(made) == 1)
        made.append((args, process))
        return process

    monkeypatch.setattr(audio.asyncio, "create_subprocess_exec", _spawn)
    assert await run_ffmpeg(["-version"]) == (0, "mean_volume: -20.0 dB\n")
    assert made[0][0] == ("ffmpeg", "-version")
    task = asyncio.ensure_future(run_ffmpeg(["-i", "x"]))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert made[1][1].killed
    assert await run_ffprobe(["-version"]) == (0, "1\n")
    assert made[2][0] == ("ffprobe", "-version")


# --- the worker's start --------------------------------------------------------


async def test_a_worker_without_ffmpeg_refuses_to_start(monkeypatch) -> None:
    monkeypatch.setattr(
        calls_worker, "select_transcribers", lambda *_: {"default": _Sides()}
    )
    built = calls_worker.worker_settings(NORMAL_QUEUE)
    ctx: dict[str, Any] = {}
    with pytest.raises(ConfigError, match="^ffmpeg_not_found$"):
        await built["on_startup"](ctx)
    assert "llm" not in ctx


async def test_the_demo_runs_without_ffmpeg_and_says_so(
    monkeypatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    monkeypatch.setenv("DODEAL_LLM_PROVIDER", "groq")
    monkeypatch.setenv("DODEAL_LLM_MODEL", "m")
    monkeypatch.setenv("DODEAL_LLM_API_KEY", "k")
    monkeypatch.setattr(calls_worker, "configure_logging", lambda: None)
    get_settings.cache_clear()
    built = calls_worker.worker_settings(NORMAL_QUEUE, transcriber=_Sides())
    ctx: dict[str, Any] = {}
    with caplog.at_level(logging.ERROR, logger="dodeal_ai.startup"):
        await built["on_startup"](ctx)
    assert ctx["audio"] is None
    assert "ffmpeg_not_found" in caplog.text
    await built["on_shutdown"](ctx)


async def test_a_worker_with_ffmpeg_holds_its_tools_and_stage2_checks_none(
    monkeypatch,
) -> None:
    async def _works(args: Sequence[str]) -> tuple[int, str]:
        return 0, "ffmpeg version 7.1"

    monkeypatch.setattr(audio, "run_ffmpeg", _works)
    monkeypatch.setattr(audio, "run_ffprobe", _works)
    monkeypatch.setenv("DODEAL_CALL_DEMO_ALLOW_LOCAL_AUDIO", "true")
    monkeypatch.setenv("DODEAL_LLM_PROVIDER", "groq")
    monkeypatch.setenv("DODEAL_LLM_MODEL", "m")
    monkeypatch.setenv("DODEAL_LLM_API_KEY", "k")
    get_settings.cache_clear()
    for queue, expected in ((NORMAL_QUEUE, True), (STAGE2_QUEUE, False)):
        built = calls_worker.worker_settings(queue, transcriber=_Sides())
        ctx: dict[str, Any] = {}
        await built["on_startup"](ctx)
        assert isinstance(ctx.get("audio"), AudioTools) is expected, queue
        await built["on_shutdown"](ctx)
