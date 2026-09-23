"""The call queues and the push onto them (register item 50).

FOUR arq queues on db0, each with a worker of its own (workers/calls.py):

  priority   calls on a lead whose status is one of the tenant's
             priority_statuses: the deals someone is waiting on
  normal     every other call
  overnight  declared, and routed to by nothing yet: the place a backlog or a
             re-analysis goes once someone decides what belongs there
  stage2     wave 2 of a done job, after its call.stage1 has gone: apart from
             the three, so no stage-2 work ever holds a stage-1 worker

The arq job carries the tenant and the job_id ONLY. The link, the hashes and
the voiceprint stay in db3 with the job, so nothing sensitive sits in db0.

The arq job id is `<tenant>:<job_id>`, so pushing the same job twice puts it on
its queue once. A pause (core(105)) re-queues under `...:resume:<n>`, the
stuck-job sweep (sweep.py) under `...:sweep:<n>`, and stage 2 goes once
under `...:stage2`. `on_the_queue` asks arq
whether any of those ids is still queued, deferred or running.
"""

from __future__ import annotations

from datetime import timedelta

import redis
from arq.constants import in_progress_key_prefix, job_key_prefix

from dodeal_ai.core import metrics
from dodeal_ai.core.breaker import queue_breaker
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.redis import get_queue_client
from dodeal_ai.units.call_intelligence.config import CallsConfig

PRIORITY_QUEUE = "arq:calls:priority"
NORMAL_QUEUE = "arq:calls:normal"
OVERNIGHT_QUEUE = "arq:calls:overnight"
STAGE2_QUEUE = "arq:calls:stage2"
CALL_QUEUES = (PRIORITY_QUEUE, NORMAL_QUEUE, OVERNIGHT_QUEUE, STAGE2_QUEUE)

# The task every call queue runs (units/call_intelligence/worker.py).
PROCESS_CALL = "process_call"
# The task that retries a callback on its schedule (delivery.py).
DELIVER_CALLBACK = "deliver_callback"
# The task the stage-2 queue runs (units/call_intelligence/stage2.py).
ANALYSE_STAGE2 = "analyse_stage2"


def queue_for(lead_status: str | None, config: CallsConfig) -> str:
    """The priority queue for a status the tenant listed, else normal. The
    status is compared casefolded, as the tenant's list is stored."""
    if lead_status is not None and lead_status.casefold() in config.priority_statuses:
        return PRIORITY_QUEUE
    return NORMAL_QUEUE


async def refresh_queue_depths() -> None:
    """call_queue_depth{queue} from each queue's length (register item 54),
    read when /metrics is served. A queue that cannot be read keeps its last
    value: the page must never fail over a gauge."""
    client = get_queue_client()
    for queue in CALL_QUEUES:

        async def _depth(name: str = queue) -> int:
            return int(await client.zcard(name))

        try:
            depth = await queue_breaker().call(_depth)
        except redis.RedisError:
            continue
        metrics.CALL_QUEUE_DEPTH.labels(queue=queue).set(depth)


def arq_job_id(tenant: str, job_id: str, *, resume: int = 0, sweep: int = 0) -> str:
    """The arq id: one per job, one per resume after a pause, and one per
    sweep of a stuck job."""
    base = f"{tenant}:{job_id}"
    if sweep:
        return f"{base}:sweep:{sweep}"
    return base if resume == 0 else f"{base}:resume:{resume}"


async def on_the_queue(tenant: str, job_id: str, *, pauses: int, sweeps: int) -> bool:
    """Whether arq holds this job under any id it can have had -- the push's,
    each resume's, each sweep's -- queued, deferred or running."""
    ids = [
        arq_job_id(tenant, job_id),
        *(arq_job_id(tenant, job_id, resume=n) for n in range(1, pauses + 1)),
        *(arq_job_id(tenant, job_id, sweep=n) for n in range(1, sweeps + 1)),
    ]
    keys = [f"{prefix}{arq_id}" for arq_id in ids for prefix in _ARQ_PREFIXES]
    client = get_queue_client()
    try:
        held = await queue_breaker().call(lambda: client.exists(*keys))
    except redis.RedisError:
        raise QueueUnavailable() from None
    return int(held) > 0


# Where arq keeps a job while it is queued or deferred, and while it runs.
_ARQ_PREFIXES = (job_key_prefix, in_progress_key_prefix)


async def enqueue_delivery(
    tenant: str,
    job_id: str,
    event: str,
    *,
    attempt: int,
    queue: str,
    defer: timedelta,
) -> None:
    """Put the `attempt`-th retry of one event on the job's queue, once."""
    client = get_queue_client()
    try:
        await queue_breaker().call(
            lambda: client.enqueue_job(
                DELIVER_CALLBACK,
                tenant,
                job_id,
                event,
                attempt,
                _job_id=f"{tenant}:{job_id}:{event}:{attempt}",
                _queue_name=queue,
                _defer_by=defer,
            )
        )
    except redis.RedisError:
        raise QueueUnavailable() from None


async def enqueue_stage2(tenant: str, job_id: str) -> None:
    """Put `analyse_stage2(tenant, job_id)` on the stage-2 queue, once. A queue
    that cannot be reached is QueueUnavailable."""
    client = get_queue_client()
    try:
        await queue_breaker().call(
            lambda: client.enqueue_job(
                ANALYSE_STAGE2,
                tenant,
                job_id,
                _job_id=f"{tenant}:{job_id}:stage2",
                _queue_name=STAGE2_QUEUE,
            )
        )
    except redis.RedisError:
        raise QueueUnavailable() from None


async def enqueue_call(
    tenant: str,
    job_id: str,
    queue: str,
    *,
    resume: int = 0,
    sweep: int = 0,
    defer: timedelta | None = None,
) -> None:
    """Put `process_call(tenant, job_id)` on `queue`, once per arq id. A queue
    that cannot be reached is QueueUnavailable; the job stays as it was."""
    client = get_queue_client()
    try:
        await queue_breaker().call(
            lambda: client.enqueue_job(
                PROCESS_CALL,
                tenant,
                job_id,
                _job_id=arq_job_id(tenant, job_id, resume=resume, sweep=sweep),
                _queue_name=queue,
                _defer_by=defer,
            )
        )
    except redis.RedisError:
        raise QueueUnavailable() from None
