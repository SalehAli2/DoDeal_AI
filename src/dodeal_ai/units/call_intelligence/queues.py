"""The call queues and the push onto them (register item 50).

THREE arq queues on db0, each with a worker of its own (workers/calls.py):

  priority   calls on a lead whose status is one of the tenant's
             priority_statuses: the deals someone is waiting on
  normal     every other call
  overnight  declared, and routed to by nothing yet: the place a backlog or a
             re-analysis goes once someone decides what belongs there

The arq job carries the tenant and the job_id ONLY. The link, the hashes and
the voiceprint stay in db3 with the job, so nothing sensitive sits in db0.

The arq job id is `<tenant>:<job_id>`, so pushing the same job twice puts it on
its queue once. A pause (core(105)) re-queues under `...:resume:<n>`.
"""

from __future__ import annotations

from datetime import timedelta

import redis

from dodeal_ai.core.breaker import queue_breaker
from dodeal_ai.core.errors import QueueUnavailable
from dodeal_ai.core.redis import get_queue_client
from dodeal_ai.units.call_intelligence.config import CallsConfig

PRIORITY_QUEUE = "arq:calls:priority"
NORMAL_QUEUE = "arq:calls:normal"
OVERNIGHT_QUEUE = "arq:calls:overnight"
CALL_QUEUES = (PRIORITY_QUEUE, NORMAL_QUEUE, OVERNIGHT_QUEUE)

# The task every call queue runs (units/call_intelligence/worker.py).
PROCESS_CALL = "process_call"


def queue_for(lead_status: str | None, config: CallsConfig) -> str:
    """The priority queue for a status the tenant listed, else normal. The
    status is compared casefolded, as the tenant's list is stored."""
    if lead_status is not None and lead_status.casefold() in config.priority_statuses:
        return PRIORITY_QUEUE
    return NORMAL_QUEUE


def arq_job_id(tenant: str, job_id: str, *, resume: int = 0) -> str:
    """The arq id: one per job, and one per resume after a pause."""
    base = f"{tenant}:{job_id}"
    return base if resume == 0 else f"{base}:resume:{resume}"


async def enqueue_call(
    tenant: str,
    job_id: str,
    queue: str,
    *,
    resume: int = 0,
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
                _job_id=arq_job_id(tenant, job_id, resume=resume),
                _queue_name=queue,
                _defer_by=defer,
            )
        )
    except redis.RedisError:
        raise QueueUnavailable() from None
