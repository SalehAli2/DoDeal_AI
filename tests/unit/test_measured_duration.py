"""The measured duration (worker.py, M11, D-91): the audio budget, the spend
and the metrics count ffprobe's seconds, rounded up, never the push's word;
the push's only when nothing measured. And untyped bytes (M12) are fetched
for ffprobe to decide."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from dodeal_ai.core import metrics
from dodeal_ai.core.jobs import JobStatus, read_job
from dodeal_ai.units.call_intelligence.audio import AudioQuality
from dodeal_ai.units.call_intelligence.worker import billed_seconds, process_call
from tests.conftest import RedisFakes
from tests.unit import test_audio_download as download_case
from tests.unit import test_call_audio as audio_case

# The recorded inspect output measures 150.0 s, mono and stereo alike.
MEASURED = 150
AUDIO_KEY = "audio_seconds:calls:tenant:tenant-a"

ctx = audio_case.ctx


def _processed() -> float:
    return metrics.AUDIO_SECONDS_PROCESSED._value.get()  # type: ignore[attr-defined]


def _spent(caplog: pytest.LogCaptureFixture) -> list[object]:
    """The audio seconds each outcome line put on the spend."""
    return [
        r.__dict__["audio_seconds"]
        for r in caplog.records
        if "audio_seconds" in r.__dict__
    ]


@pytest.mark.parametrize(
    ("declared", "measured", "billed"),
    [(150, None, 150), (60, 150.0, 150), (900, 149.2, 150), (10, 0.4, 1)],
)
def test_the_measure_rounded_up_is_billed(
    declared: int, measured: float | None, billed: int
) -> None:
    audio = None if measured is None else AudioQuality(measured, 1, 0.9, -20.0)
    assert billed_seconds(declared, audio) == billed


@pytest.mark.parametrize(
    ("channels", "declared", "charged"),
    [("mono", 60, MEASURED), ("mono", 900, MEASURED), ("stereo", 60, 2 * MEASURED)],
)
async def test_budget_spend_and_metrics_count_the_measured_seconds(
    ctx: dict[str, Any],
    redis_fakes: RedisFakes,
    caplog: pytest.LogCaptureFixture,
    channels: str,
    declared: int,
    charged: int,
) -> None:
    """A push that says 60 s, or 900 s, of a 150 s recording is charged 150 s
    per side transcribed."""
    if channels == "stereo":
        ctx["audio"] = audio_case._tools(audio_case._Ffmpeg("stereo"))
        await audio_case._calls_on(audio_channels="stereo_agent_left")
    else:
        await audio_case._calls_on()
    await audio_case._push(duration=declared)
    before = _processed()
    with caplog.at_level(logging.INFO, logger="dodeal_ai.unit_b"):
        await process_call(ctx, "tenant-a", audio_case.JOB)
    job = await read_job("tenant-a", audio_case.JOB)
    assert job is not None and job.status is JobStatus.DONE
    assert int(redis_fakes.cost.store[AUDIO_KEY]) == charged
    assert _processed() - before == MEASURED
    assert _spent(caplog) == [charged]


@pytest.mark.parametrize("media", ["application/octet-stream", "binary/octet-stream"])
async def test_untyped_bytes_are_fetched_for_ffprobe_to_decide(media: str) -> None:
    source = download_case._Source(
        lambda _: download_case._audio(**{"content-type": f"{media}; x=1"})
    )
    body, size, fetched = await download_case._fetch(source)
    assert (body, size, fetched) == (download_case.AUDIO, len(body), media)


@pytest.mark.parametrize("media", ["text/plain", "application/json", "video/mp4"])
async def test_other_types_are_still_refused(media: str) -> None:
    refusal = await download_case._refusal(
        download_case._Source(lambda _: download_case._audio(**{"content-type": media}))
    )
    assert refusal.reason == "audio_type_refused"
