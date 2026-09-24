"""The call workers (Unit B, register items 35 and 50): one arq worker per call
queue, running process_call, and one on the stage-2 queue, running
analyse_stage2. THE worker entry point; there is no other.

    python -m dodeal_ai.workers.calls priority
    python -m dodeal_ai.workers.calls normal
    python -m dodeal_ai.workers.calls overnight
    python -m dodeal_ai.workers.calls stage2

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
passes one in, and the worker starts with it only under the demo flag. The
stage-2 worker transcribes nothing and builds none.

THE MODEL CLIENT IS BUILT AS THE SERVICE BUILDS IT: core/llm's
build_llm_client and build_router, from the same DODEAL_LLM_* settings, registry and routes,
with the same startup sweep of every configured profile and one pooled client
per provider. There is no second configuration. Unlike the
service, which starts without a provider so /ready can say so, a call worker
with no model client refuses to start: it would pay for transcripts and deliver
every one with analysis null. Wave 1's templates are read once here too, so a
missing one refuses the start rather than a paid call.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx
from arq import cron, func
from arq.connections import RedisSettings
from arq.worker import run_worker

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import aclose_llm, build_llm_client, build_router
from dodeal_ai.core.logging_config import configure_logging, warn_if_demo_audio
from dodeal_ai.core.prompting import clear_templates, preload_templates
from dodeal_ai.core.redis import redis_url
from dodeal_ai.units.call_intelligence.delivery import (
    deliver_callback,
    deliver_event,
)
from dodeal_ai.units.call_intelligence.prompts import UNIT_B_TEMPLATES
from dodeal_ai.units.call_intelligence.queues import (
    NORMAL_QUEUE,
    OVERNIGHT_QUEUE,
    PRIORITY_QUEUE,
    STAGE2_QUEUE,
)
from dodeal_ai.units.call_intelligence.stage2 import STAGE2_TRIES, analyse_stage2
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
    "stage2": STAGE2_QUEUE,
}


def redis_settings() -> RedisSettings:
    """arq's Redis settings, parsed from the queue URL (read through
    core/redis.py's one read of it); opens nothing."""
    return RedisSettings.from_dsn(redis_url("queue"))


def worker_settings(
    queue: str, *, transcriber: Transcriber | None = None
) -> dict[str, Any]:
    """The arq settings for one call queue. `transcriber` is for the demo and
    the tests; a deployment gets the factory's."""
    settings = get_settings()

    async def startup(ctx: dict[str, Any]) -> None:
        configure_logging()
        warn_if_demo_audio(settings)
        if queue != STAGE2_QUEUE:
            ctx["transcriber"] = select_transcriber(settings, transcriber)
        preload_templates(UNIT_B_TEMPLATES)
        # The model's own pool, apart from the download's pinned client.
        llm_http = httpx.AsyncClient()
        try:
            ctx["llm"] = await build_router(
                settings, build_llm_client(settings, llm_http)
            )
        except BaseException:
            await llm_http.aclose()
            clear_templates()
            raise
        ctx["llm_http"] = llm_http
        # No proxies from the environment and no redirects: the download pins
        # the address it checked, and a proxy would bypass that.
        ctx["http"] = httpx.AsyncClient(trust_env=False, follow_redirects=False)
        # Signed callbacks to the tenant's URL, retried on their own schedule.
        ctx["deliver"] = lambda job, event, config: deliver_event(
            ctx, job, event, config
        )

    async def shutdown(ctx: dict[str, Any]) -> None:
        await aclose_llm(ctx.get("llm"))
        for name in ("http", "llm_http"):
            client = ctx.get(name)
            if client is not None:
                await client.aclose()
        clear_templates()

    sweeps = (
        [cron(sweep_stuck_jobs, minute=_every(SWEEP_INTERVAL_SECONDS))]
        if queue == NORMAL_QUEUE
        else []
    )
    functions = (
        [func(analyse_stage2, max_tries=STAGE2_TRIES)]
        if queue == STAGE2_QUEUE
        else [
            func(process_call, max_tries=settings.call_max_tries + 1),
            # One re-run for a worker that died mid-POST; the schedule is ours.
            func(deliver_callback, max_tries=2),
        ]
    )
    return {
        "cron_jobs": sweeps,
        "functions": functions,
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
