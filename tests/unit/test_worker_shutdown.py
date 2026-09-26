"""The call workers' shutdown wait and job limits (workers/calls.py): each
queue's CALL_MAX_JOBS_<QUEUE> and CALL_JOB_COMPLETION_WAIT_SECONDS reach the
arq Worker built from worker_settings, defaults and overrides alike."""

from __future__ import annotations

import signal

import pytest
from arq.worker import Worker
from pydantic import ValidationError

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.units.call_intelligence.queues import (
    NORMAL_QUEUE,
    OVERNIGHT_QUEUE,
    PRIORITY_QUEUE,
    STAGE2_QUEUE,
)
from dodeal_ai.workers import calls as calls_worker

DEFAULT_MAX_JOBS = {
    PRIORITY_QUEUE: 4,
    NORMAL_QUEUE: 4,
    OVERNIGHT_QUEUE: 2,
    STAGE2_QUEUE: 8,
}


def _arq_worker(queue: str) -> Worker:
    """The Worker arq builds from our settings; opens no connection."""
    return Worker(**calls_worker.worker_settings(queue), handle_signals=False)


def _slots(worker: Worker) -> int:
    """arq keeps max_jobs only as its job semaphore's size."""
    return worker.sem._value  # type: ignore[attr-defined]


@pytest.mark.parametrize("queue", list(DEFAULT_MAX_JOBS))
async def test_the_defaults_reach_arq(queue: str) -> None:
    get_settings.cache_clear()
    worker = _arq_worker(queue)
    assert _slots(worker) == DEFAULT_MAX_JOBS[queue]
    assert worker._job_completion_wait == 660


async def test_each_setting_reaches_its_own_queue_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DODEAL_CALL_MAX_JOBS_PRIORITY", "5")
    monkeypatch.setenv("DODEAL_CALL_MAX_JOBS_NORMAL", "6")
    monkeypatch.setenv("DODEAL_CALL_MAX_JOBS_OVERNIGHT", "1")
    monkeypatch.setenv("DODEAL_CALL_MAX_JOBS_STAGE2", "9")
    monkeypatch.setenv("DODEAL_CALL_JOB_COMPLETION_WAIT_SECONDS", "90")
    get_settings.cache_clear()
    expected = {PRIORITY_QUEUE: 5, NORMAL_QUEUE: 6, OVERNIGHT_QUEUE: 1, STAGE2_QUEUE: 9}
    for queue, limit in expected.items():
        worker = _arq_worker(queue)
        assert _slots(worker) == limit
        assert worker._job_completion_wait == 90
    get_settings.cache_clear()


async def test_a_signal_lets_running_jobs_finish() -> None:
    """With a wait set, arq's SIGTERM handler stops taking jobs and waits,
    rather than cancelling the running ones at once."""
    get_settings.cache_clear()
    worker = Worker(**calls_worker.worker_settings(NORMAL_QUEUE))
    try:
        handlers = worker.loop._signal_handlers  # type: ignore[attr-defined]
        for signum in (signal.SIGTERM, signal.SIGINT):
            called = handlers[signum]._callback.func
            assert called == worker.handle_sig_wait_for_completion
    finally:
        worker.loop.remove_signal_handler(signal.SIGINT)
        worker.loop.remove_signal_handler(signal.SIGTERM)


@pytest.mark.parametrize(
    "field",
    [
        "call_max_jobs_priority",
        "call_max_jobs_normal",
        "call_max_jobs_overnight",
        "call_max_jobs_stage2",
    ],
)
def test_a_worker_with_no_job_slot_is_refused(field: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, jwt_signing_key="k", **{field: 0})
    assert [e["loc"] for e in caught.value.errors()] == [(field,)]


def test_a_negative_wait_is_refused() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None, jwt_signing_key="k", call_job_completion_wait_seconds=-1
        )
