"""arq's own failure lines (core/logging_config.py, M10): a job that raised
is logged by arq with the exception among its message's arguments; on our
handler that line keeps the exception's class, type and frames, never its
message. Tested with a sentinel message through a real arq worker."""

from __future__ import annotations

import json
import logging
from typing import Any, ClassVar

import pytest
from arq import func
from arq.worker import Worker

from dodeal_ai.core.logging_config import configure_logging
from tests.conftest import RedisFakes
from tests.unit.test_logging_config import _restore_logging_state  # noqa: F401

SENTINEL = "sentinel-note-text-0f3c"


class _Foreign(Exception):
    """Raised with data in its message, as a library's error would be."""

    extra: ClassVar[dict[str, str]] = {"payload": SENTINEL}


async def _boom(ctx: dict[str, Any]) -> None:
    raise _Foreign(f"the model said {SENTINEL}")


def _lines(out: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.strip().splitlines() if line]


async def test_a_failed_jobs_line_never_carries_the_message(
    redis_fakes: RedisFakes, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_info(*args: Any) -> None:
        """fakeredis has no INFO; arq only logs it at start."""

    monkeypatch.setattr("arq.worker.log_redis_info", no_info)
    configure_logging()
    worker = Worker(
        functions=[func(_boom, name="boom")],
        redis_pool=redis_fakes.queue,
        burst=True,
        handle_signals=False,
        poll_delay=0.01,
    )
    await redis_fakes.queue.enqueue_job("boom", _job_id="job-1")
    await worker.main()
    out = capsys.readouterr().out
    assert SENTINEL not in out
    (failed,) = [line for line in _lines(out) if line["logger"] == "arq.worker"]
    assert failed["level"] == "ERROR"
    assert failed["message"].endswith("job-1:boom failed, _Foreign: _Foreign")
    assert failed["exc_type"].endswith("._Foreign")
    assert failed["exc_frames"] and "extra" not in failed


def test_only_arq_lines_are_rewritten(capsys: Any) -> None:
    """Our own lines keep their arguments: log_safety decides those."""
    configure_logging()
    error = ValueError("kept")
    logging.getLogger("dodeal_ai.test").warning("ours %s", error)
    logging.getLogger("arq.jobs").warning("theirs %s", error)
    ours, theirs = _lines(capsys.readouterr().out)
    assert (ours["message"], theirs["message"]) == ("ours kept", "theirs ValueError")
