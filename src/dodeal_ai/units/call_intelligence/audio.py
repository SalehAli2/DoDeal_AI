"""The audio layer (Unit B): what is known about a recording before any paid
call, and the one format every engine is sent. Engine-independent, on ffmpeg
and ffprobe, both checked at worker start.

  probe      ffprobe decodes an audio frame from the file's first seconds, or
             it is audio_format_unknown, whatever its container claims
  inspect    one ffmpeg pass over the file: its duration, its channels, the
             speech ratio (1 less the share silencedetect finds silent) and
             the mean loudness (volumedetect)
  converted  the recording as one 16 kHz mono FLAC file, its channels mixed
  split      a stereo recording as two 16 kHz mono FLAC files, one per side
  merge      the two sides' transcripts into one, by time, as agent and client

EVERY ENGINE IS SENT 16 kHz MONO FLAC (ENGINE_MIME), written to a private
directory deleted however the caller leaves the block: no engine is left to
guess a container.

THE QUALITY FLOORS ARE PROVISIONAL (BRD B12): a speech ratio under 0.30 or a
mean volume under -40 dB makes the transcript uncertain whatever the engine
says, and a number ffmpeg did not report counts as under its floor. Silence is
anything quieter than the volume floor for half a second or more.

THE LONGEST CALL is 3600 s: over it, audio_too_long, a permanent failure. A
file ffprobe cannot decode is audio_format_unknown, one ffmpeg cannot read or
convert audio_unreadable, and a stereo setting on a file that is not two
channels audio_channels_mismatch; all permanent, before any paid call.

ffmpeg and ffprobe run as async subprocesses, killed if their caller is
cancelled. Their output is parsed for numbers and never logged: it names the
file.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dodeal_ai.core.config import ConfigError
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript

# The longest call transcribed, in seconds; longer is audio_too_long.
MAX_CALL_SECONDS = 3600

# Provisional floors (BRD B12): under either, the transcript is uncertain.
MIN_SPEECH_RATIO = 0.30
MIN_MEAN_VOLUME_DB = -40.0

# silencedetect's threshold and shortest silence: quieter than the volume
# floor for this long is not speech.
SILENCE_MIN_SECONDS = 0.5

# What every engine is sent: FLAC, mono, at this rate. Lossless, a fraction of
# a WAV's size, and a format every engine documents.
ENGINE_SAMPLE_RATE = 16_000
ENGINE_MIME = "audio/flac"
_FLAC = ("-c:a", "flac", "-ar", str(ENGINE_SAMPLE_RATE))

# How many seconds from the start ffprobe decodes to prove it can: past any
# decoder's start-up delay, and never the whole file.
PROBE_SECONDS = 5

# The permanent refusals of a file itself, before any paid call.
UNKNOWN_FORMAT = "audio_format_unknown"
UNREADABLE = "audio_unreadable"

# The reasons a transcript is uncertain on its audio alone.
LOW_SPEECH = "low_speech_ratio"
LOW_VOLUME = "low_volume"

# Decimal places kept, so the payload carries no float noise.
_PLACES = 3

type AudioChannels = Literal["mono", "stereo_agent_left", "stereo_agent_right"]

# One ffmpeg run: its arguments in, its exit code and its stderr text out. An
# ffprobe run hands back its stdout instead, where it prints what was asked.
type FfmpegRunner = Callable[[Sequence[str]], Awaitable[tuple[int, str]]]

_DURATION = re.compile(r"Duration:\s*(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
_CHANNELS = re.compile(r"Audio:.*?\d+ Hz,\s*(mono|stereo|(\d+) channels)")
_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_duration:\s*(\d+(?:\.\d+)?)")
_MEAN_VOLUME = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB")


class AudioError(Exception):
    """A recording this layer refuses. str() is the fixed reason code; never
    a path or a line of ffmpeg's output."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class FfmpegMissing(ConfigError):
    """ffmpeg or ffprobe is not on this worker's path, or will not run."""

    def __init__(self) -> None:
        super().__init__("ffmpeg_not_found")


@dataclass(frozen=True, slots=True)
class AudioQuality:
    """One recording's numbers: None where ffmpeg reported none."""

    duration_seconds: float
    channels: int | None
    speech_ratio: float | None
    mean_volume_db: float | None

    def reasons(self) -> tuple[str, ...]:
        """Why the audio alone makes a transcript uncertain; () when it does not."""
        found = []
        if self.speech_ratio is None or self.speech_ratio < MIN_SPEECH_RATIO:
            found.append(LOW_SPEECH)
        if self.mean_volume_db is None or self.mean_volume_db < MIN_MEAN_VOLUME_DB:
            found.append(LOW_VOLUME)
        return tuple(found)

    def to_dict(self) -> dict[str, object]:
        return {
            "duration_seconds": self.duration_seconds,
            "channels": self.channels,
            "speech_ratio": self.speech_ratio,
            "mean_volume_db": self.mean_volume_db,
        }

    @classmethod
    def from_dict(cls, kept: dict[str, object]) -> AudioQuality:
        def number(name: str) -> float | None:
            value = kept.get(name)
            return None if value is None else float(str(value))

        channels = kept.get("channels")
        return cls(
            duration_seconds=float(str(kept["duration_seconds"])),
            channels=None if channels is None else int(str(channels)),
            speech_ratio=number("speech_ratio"),
            mean_volume_db=number("mean_volume_db"),
        )


async def run_ffmpeg(args: Sequence[str]) -> tuple[int, str]:
    """ffmpeg with `args`: its exit code and stderr. Killed if cancelled."""
    return await _run("ffmpeg", args, stdout=False)


async def run_ffprobe(args: Sequence[str]) -> tuple[int, str]:
    """ffprobe with `args`: its exit code and stdout. Killed if cancelled."""
    return await _run("ffprobe", args, stdout=True)


async def _run(program: str, args: Sequence[str], *, stdout: bool) -> tuple[int, str]:
    """`program` with `args`: its exit code and the one output asked for."""
    pipe, devnull = asyncio.subprocess.PIPE, asyncio.subprocess.DEVNULL
    process = await asyncio.create_subprocess_exec(
        program,
        *args,
        stdin=devnull,
        stdout=pipe if stdout else devnull,
        stderr=devnull if stdout else pipe,
    )
    try:
        out, err = await process.communicate()
    finally:
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()
    text = out if stdout else err
    return int(process.returncode or 0), text.decode("utf-8", errors="replace")


def _seconds(match: re.Match[str]) -> float:
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def parse_quality(output: str) -> AudioQuality:
    """The numbers in one inspect run's output; AudioError when it has no
    duration, which is a file ffmpeg could not read."""
    duration = _DURATION.search(output)
    if duration is None:
        raise AudioError(UNREADABLE)
    seconds = _seconds(duration)
    channels_match = _CHANNELS.search(output)
    channels = (
        None
        if channels_match is None
        else {"mono": 1, "stereo": 2}.get(channels_match.group(1))
        or int(channels_match.group(2))
    )
    silent = sum(float(value) for value in _SILENCE_END.findall(output))
    starts = _SILENCE_START.findall(output)
    ends = _SILENCE_END.findall(output)
    if len(starts) > len(ends):
        # A silence still open at the end runs to the end of the file.
        silent += max(seconds - float(starts[-1]), 0.0)
    ratio = None if seconds <= 0 else max(0.0, 1.0 - silent / seconds)
    volume = _MEAN_VOLUME.search(output)
    return AudioQuality(
        duration_seconds=round(seconds, _PLACES),
        channels=channels,
        speech_ratio=None if ratio is None else round(ratio, _PLACES),
        mean_volume_db=None if volume is None else float(volume.group(1)),
    )


class AudioTools:
    """ffmpeg and ffprobe, as this layer runs them: built by `ensure_ffmpeg` at
    worker start on `run_ffmpeg` and `run_ffprobe`, or by a test on runners of
    its own."""

    def __init__(self, runner: FfmpegRunner, probe: FfmpegRunner) -> None:
        self._run = runner
        self._probe = probe

    async def probe(self, path: Path) -> None:
        """AudioError(audio_format_unknown) unless ffprobe decodes at least one
        frame of the first audio stream within the first PROBE_SECONDS."""
        code, output = await self._probe(
            [
                *("-v", "error", "-select_streams", "a:0"),
                *("-read_intervals", f"%+{PROBE_SECONDS}", "-count_frames"),
                *("-show_entries", "stream=nb_read_frames"),
                *("-of", "default=noprint_wrappers=1:nokey=1", str(path)),
            ]
        )
        frames = output.strip()
        if code != 0 or not frames.isdecimal() or int(frames) < 1:
            raise AudioError(UNKNOWN_FORMAT)

    async def inspect(self, path: Path) -> AudioQuality:
        """Duration, channels, speech ratio and mean volume, in one pass."""
        noise = f"{MIN_MEAN_VOLUME_DB:g}dB"
        code, output = await self._run(
            [
                "-hide_banner",
                "-nostats",
                "-i",
                str(path),
                "-af",
                f"silencedetect=noise={noise}:d={SILENCE_MIN_SECONDS},volumedetect",
                "-f",
                "null",
                "-",
            ]
        )
        if code != 0:
            raise AudioError(UNREADABLE)
        return parse_quality(output)

    @asynccontextmanager
    async def converted(self, path: Path) -> AsyncIterator[Path]:
        """The first audio stream as one 16 kHz mono FLAC file, its channels
        mixed, deleted with its directory when the block exits."""
        async with _scratch() as directory:
            flac = directory / "call.flac"
            code, _ = await self._run(
                [
                    *("-hide_banner", "-nostats", "-y", "-i", str(path)),
                    *("-map", "0:a:0", "-ac", "1", *_FLAC, str(flac)),
                ]
            )
            if code != 0:
                raise AudioError(UNREADABLE)
            yield flac

    @asynccontextmanager
    async def split(self, path: Path) -> AsyncIterator[tuple[Path, Path]]:
        """The left and right channels as two 16 kHz mono FLAC files, deleted
        with their directory when the block exits, however it exits."""
        async with _scratch() as directory:
            left, right = directory / "left.flac", directory / "right.flac"
            code, _ = await self._run(
                [
                    *("-hide_banner", "-nostats", "-y", "-i", str(path)),
                    "-filter_complex",
                    "[0:a]pan=mono|c0=c0[l];[0:a]pan=mono|c0=c1[r]",
                    *("-map", "[l]", "-ac", "1", *_FLAC, str(left)),
                    *("-map", "[r]", "-ac", "1", *_FLAC, str(right)),
                ]
            )
            if code != 0:
                raise AudioError(UNREADABLE)
            yield left, right


@asynccontextmanager
async def _scratch() -> AsyncIterator[Path]:
    """A private directory, deleted with its files however the block exits."""
    directory = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="call-"))
    try:
        yield directory
    finally:
        await asyncio.to_thread(shutil.rmtree, directory, ignore_errors=True)


async def ensure_ffmpeg(
    runner: FfmpegRunner | None = None, probe: FfmpegRunner | None = None
) -> AudioTools:
    """The worker's AudioTools once `ffmpeg -version` and `ffprobe -version`
    run; FfmpegMissing when either cannot, so a worker without them never
    starts to fail every call."""
    runner = run_ffmpeg if runner is None else runner
    probe = run_ffprobe if probe is None else probe
    for tool in (runner, probe):
        try:
            code, _ = await tool(["-hide_banner", "-version"])
        except OSError:
            raise FfmpegMissing() from None
        if code != 0:
            raise FfmpegMissing()
    return AudioTools(runner, probe)


def side_roles(channels: AudioChannels) -> tuple[str, str]:
    """The roles of the left and right sides, in that order."""
    if channels == "stereo_agent_right":
        return ("client", "agent")
    return ("agent", "client")


def merge_sides(
    sides: Sequence[tuple[str, Transcript]], *, provider: str, model: str
) -> Transcript:
    """One transcript from each side's, every segment named by its side's role
    and ordered by time. Crosstalk cannot overlap in a transcript, so a segment
    that starts inside the one before it starts where that one ends (a segment
    wholly inside it keeps its words at no length)."""
    ordered = sorted(
        (
            (segment.start_s, segment.end_s, index, role, segment)
            for role, transcript in sides
            for index, segment in enumerate(transcript.segments)
        ),
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )
    merged: list[Segment] = []
    for start, end, _, role, segment in ordered:
        floor = merged[-1].end_s if merged else 0.0
        begin = max(start, floor)
        merged.append(
            segment.model_copy(
                update={"speaker": role, "start_s": begin, "end_s": max(end, begin)}
            )
        )
    return Transcript.of(tuple(merged), provider=provider, model=model)
