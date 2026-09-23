"""The call workers (Unit B, register items 35 and 50): one arq worker per call
queue, running process_call. THE worker entry point; there is no other.

    python -m dodeal_ai.workers.calls priority
    python -m dodeal_ai.workers.calls normal
    python -m dodeal_ai.workers.calls overnight

BUILT BY A FUNCTION, NOT A CLASS BODY. arq reads a settings class through its
`__dict__`, so a lazy descriptor reaches arq as the descriptor itself; and a
class body that read Settings would read them on import, which the wheel check
and pytest collection forbid. `worker_settings(queue)` reads Settings when the
worker is built and hands arq a plain dict.

arq's own try count is CALL_MAX_TRIES + 1, one more than the job's: the job's
attempts in db3 decide, and the extra run is the one that dead-letters a job
whose last attempt crashed. Each run is cut off at CALL_JOB_TIMEOUT_SECONDS.

THE NORMAL QUEUE'S WORKER ALSO SWEEPS, every 300 s: a job stuck in a running
status with no run is re-enqueued once (units/call_intelligence/sweep.py).
arq runs a cron once per slot however many normal workers there are.

THE TRANSCRIBER IS BUILT BY THE FACTORY, which refuses while no adapter exists,
so a worker with nothing to transcribe with does not start. A test or the demo
passes one in, and the worker starts with it only under the demo flag.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
from arq import cron, func
from arq.connections import RedisSettings
from arq.worker import run_worker

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.logging_config import configure_logging, warn_if_demo_audio
from dodeal_ai.units.call_intelligence.delivery import (
    deliver_callback,
    deliver_event,
)
from dodeal_ai.units.call_intelligence.queues import (
    NORMAL_QUEUE,
    OVERNIGHT_QUEUE,
    PRIORITY_QUEUE,
)
from dodeal_ai.units.call_intelligence.sweep import (
    SWEEP_INTERVAL_SECONDS,
    sweep_stuck_jobs,
)
from dodeal_ai.units.call_intelligence.transcriber import (
    Transcriber,
    select_transcriber,
)
from dodeal_ai.units.call_intelligence.worker import process_call

QUEUES = {
    "priority": PRIORITY_QUEUE,
    "normal": NORMAL_QUEUE,
    "overnight": OVERNIGHT_QUEUE,
}


def redis_settings() -> RedisSettings:
    """arq's Redis settings, parsed from redis_queue_url; opens nothing."""
    return RedisSettings.from_dsn(get_settings().redis_queue_url)


def worker_settings(
    queue: str, *, transcriber: Transcriber | None = None
) -> dict[str, Any]:
    """The arq settings for one call queue. `transcriber` is for the demo and
    the tests; a deployment gets the factory's."""
    settings = get_settings()

    async def startup(ctx: dict[str, Any]) -> None:
        configure_logging()
        warn_if_demo_audio(settings)
        ctx["transcriber"] = select_transcriber(settings, transcriber)
        # No proxies from the environment and no redirects: the download pins
        # the address it checked, and a proxy would bypass that.
        ctx["http"] = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        # Signed callbacks to the tenant's URL, retried on their own schedule.
        ctx["deliver"] = lambda job, event, config: deliver_event(
            ctx, job, event, config
        )

    async def shutdown(ctx: dict[str, Any]) -> None:
        http = ctx.get("http")
        if http is not None:
            await http.aclose()

    sweeps = (
        [cron(sweep_stuck_jobs, minute=_every(SWEEP_INTERVAL_SECONDS))]
        if queue == NORMAL_QUEUE
        else []
    )
    return {
        "cron_jobs": sweeps,
        "functions": [
            func(process_call, max_tries=settings.call_max_tries + 1),
            # One re-run for a worker that died mid-POST; the schedule is ours.
            func(deliver_callback, max_tries=2),
        ],
        "queue_name": queue,
        "job_timeout": settings.call_job_timeout_seconds,
        "redis_settings": redis_settings(),
        "on_startup": startup,
        "on_shutdown": shutdown,
    }


def _every(seconds: int) -> set[int]:
    """The minutes of the hour a cron every `seconds` fires on."""
    return set(range(0, 60, seconds // 60))


def main(argv: list[str]) -> int:
    """Run the worker for the queue named on the command line."""
    if len(argv) != 1 or argv[0] not in QUEUES:
        print(f"usage: python -m dodeal_ai.workers.calls {'|'.join(QUEUES)}")
        return 2
    run_worker(worker_settings(QUEUES[argv[0]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
