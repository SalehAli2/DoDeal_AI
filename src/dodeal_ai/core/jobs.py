"""Unit B's call jobs in db3 (register item 50): one job per (tenant, job_id),
at most one per (tenant, call_id), and its result held apart from it.

THREE KEYS, every one under the tenant, so a job id from one tenant can never
address another tenant's job:

  call_job:{tenant}:{job_id}          a hash: status, attempts, pauses, reason,
                                      request_id, queue, timestamps, metadata
  call_job_by_call:{tenant}:{call_id} the job_id this call was admitted as
  call_result:{tenant}:{job_id}       the stage result, EX result_ttl_seconds

ONE JOB PER CALL is one Lua script: the index is read and, only if absent, the
index and the job are written together. Two concurrent pushes of one call
therefore make one job, and the second is told the first's job_id.

TERMINAL IS FINAL. done, failed and dead_letter refuse every later transition,
claim and pause inside the same scripts, and on reaching one the job hash and
its index take the tenant's result_ttl_seconds, so a finished job and its
dedupe entry expire together. A job still running holds no TTL.

FAILS CLOSED. The job store is the job: a store that cannot be read or written
raises JobStoreUnavailable (a 503 at the route, a retry in the worker), never a
guess. Every call runs inside the jobs breaker and catches RedisError.

`metadata` holds the push body's fields, the signed audio link among them. It
is never logged, never on a repr, and never in an exception.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import redis

from dodeal_ai.core.breaker import jobs_breaker
from dodeal_ai.core.redis import get_jobs_client


class JobStatus(StrEnum):
    """Where a job is. The vocabulary the CRM reads on GET; fixed."""

    QUEUED = "queued"
    DOWNLOADING = "downloading"
    TRANSCRIBING = "transcribing"
    ANALYSING = "analysing"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"
    PAUSED_BUDGET = "paused_budget"
    DEAD_LETTER = "dead_letter"


# The statuses no transition, claim or pause may leave.
TERMINAL: frozenset[JobStatus] = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD_LETTER}
)


class JobStoreUnavailable(Exception):
    """db3 could not be read or written. Fixed text: never a key or a value."""

    def __init__(self) -> None:
        super().__init__("job_store_unavailable")


@dataclass(frozen=True, slots=True)
class Job:
    """One job as stored. `metadata` is off the repr: it holds a signed link."""

    tenant: str
    job_id: str
    call_id: int
    status: JobStatus
    attempts: int
    pauses: int
    request_id: str
    reason: str | None
    queue: str
    created_at: str
    updated_at: str
    finished_at: str | None
    metadata: dict[str, object] = field(repr=False)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


def job_key(tenant: str, job_id: str) -> str:
    return f"call_job:{tenant}:{job_id}"


def call_index_key(tenant: str, call_id: int) -> str:
    return f"call_job_by_call:{tenant}:{call_id}"


def result_key(tenant: str, job_id: str) -> str:
    return f"call_result:{tenant}:{job_id}"


# KEYS: the call index, the new job. ARGV: the new job_id, then field/value
# pairs. Returns {1, job_id} when this call made the job, {0, the existing
# job_id} when the call already had one; nothing is written then.
_CREATE_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then
  return {0, existing}
end
redis.call('SET', KEYS[1], ARGV[1])
for i = 2, #ARGV, 2 do
  redis.call('HSET', KEYS[2], ARGV[i], ARGV[i + 1])
end
return {1, ARGV[1]}
"""

# KEYS: the job, the call index. ARGV: status, now, reason, ttl, "1" when the
# status is terminal, then every terminal status. 0 missing, -1 already
# terminal, 1 moved; a terminal move sets finished_at and the TTL on both keys.
_TRANSITION_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 6, #ARGV do
  if current == ARGV[i] then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'status', ARGV[1], 'updated_at', ARGV[2], 'reason', ARGV[3])
if ARGV[5] == '1' then
  redis.call('HSET', KEYS[1], 'finished_at', ARGV[2])
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  redis.call('EXPIRE', KEYS[2], ARGV[4])
end
return 1
"""

# KEYS: the job. ARGV: max tries, now, the running status, then every terminal
# status. {-2, 0} missing, {-1, 0} terminal, {0, attempts} at the cap (nothing
# moves), {1, attempts} claimed: attempts incremented and the reason cleared.
_CLAIM_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-2, 0}
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 4, #ARGV do
  if current == ARGV[i] then
    return {-1, 0}
  end
end
local attempts = tonumber(redis.call('HGET', KEYS[1], 'attempts') or '0')
if attempts >= tonumber(ARGV[1]) then
  return {0, attempts}
end
attempts = redis.call('HINCRBY', KEYS[1], 'attempts', 1)
redis.call('HSET', KEYS[1], 'status', ARGV[3], 'updated_at', ARGV[2], 'reason', '')
return {1, attempts}
"""

# KEYS: the job. ARGV: the paused status, now, reason, then every terminal
# status. 0 missing, -1 terminal, else the job's pause count after this one.
_PAUSE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 4, #ARGV do
  if current == ARGV[i] then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'status', ARGV[1], 'updated_at', ARGV[2], 'reason', ARGV[3])
return redis.call('HINCRBY', KEYS[1], 'pauses', 1)
"""

_TERMINAL_ARGS = tuple(sorted(status.value for status in TERMINAL))


def _text(value: str | bytes) -> str:
    """A hash value as text; the client decodes already (decode_responses)."""
    return value.decode() if isinstance(value, bytes) else value


async def _call[T](factory: Callable[[], Awaitable[T]]) -> T:
    """One db3 command inside the jobs breaker; any RedisError fails closed."""
    try:
        return await jobs_breaker().call(factory)
    except redis.RedisError:
        raise JobStoreUnavailable() from None


async def create_job(
    tenant: str,
    call_id: int,
    *,
    job_id: str,
    request_id: str,
    queue: str,
    metadata: dict[str, object],
    now: datetime,
) -> tuple[str, bool]:
    """Admit `call_id` as `job_id`, queued, unless the call already has a job.
    Returns (the call's job_id, whether this call made it)."""
    stamp = now.isoformat()
    fields = {
        "tenant": tenant,
        "job_id": job_id,
        "call_id": str(call_id),
        "status": JobStatus.QUEUED.value,
        "attempts": "0",
        "pauses": "0",
        "request_id": request_id,
        "reason": "",
        "queue": queue,
        "created_at": stamp,
        "updated_at": stamp,
        "finished_at": "",
        "metadata": json.dumps(metadata, sort_keys=True),
    }
    pairs = [item for pair in fields.items() for item in pair]
    client = get_jobs_client()
    made, existing = await _call(
        lambda: client.eval(
            _CREATE_SCRIPT,
            2,
            call_index_key(tenant, call_id),
            job_key(tenant, job_id),
            job_id,
            *pairs,
        )
    )
    return str(existing), int(made) == 1


async def read_job(tenant: str, job_id: str) -> Job | None:
    """The job, or None when this tenant has no such job (or it expired)."""
    client = get_jobs_client()
    stored = await _call(lambda: client.hgetall(job_key(tenant, job_id)))
    if not stored:
        return None
    raw = {str(name): _text(value) for name, value in stored.items()}
    return Job(
        tenant=raw["tenant"],
        job_id=raw["job_id"],
        call_id=int(raw["call_id"]),
        status=JobStatus(raw["status"]),
        attempts=int(raw["attempts"]),
        pauses=int(raw["pauses"]),
        request_id=raw["request_id"],
        reason=raw["reason"] or None,
        queue=raw["queue"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        finished_at=raw["finished_at"] or None,
        metadata=json.loads(raw["metadata"]),
    )


async def transition(
    job: Job,
    status: JobStatus,
    *,
    now: datetime,
    reason: str | None = None,
    ttl_seconds: int,
) -> bool:
    """Move a job that is not terminal to `status`. A terminal status sets
    finished_at and `ttl_seconds` on the job and its call index. False when the
    job is gone or already terminal: nothing moved."""
    client = get_jobs_client()
    moved = await _call(
        lambda: client.eval(
            _TRANSITION_SCRIPT,
            2,
            job_key(job.tenant, job.job_id),
            call_index_key(job.tenant, job.call_id),
            status.value,
            now.isoformat(),
            reason or "",
            ttl_seconds,
            "1" if status in TERMINAL else "0",
            *_TERMINAL_ARGS,
        )
    )
    return int(moved) == 1


async def claim_attempt(
    job: Job, *, max_tries: int, now: datetime, status: JobStatus
) -> int | None:
    """Start one processing attempt: attempts + 1 and `status`, atomically,
    unless the job is terminal or gone (None) or at `max_tries` (0)."""
    client = get_jobs_client()
    outcome, attempts = await _call(
        lambda: client.eval(
            _CLAIM_SCRIPT,
            1,
            job_key(job.tenant, job.job_id),
            max_tries,
            now.isoformat(),
            status.value,
            *_TERMINAL_ARGS,
        )
    )
    if int(outcome) < 0:
        return None
    return int(attempts) if int(outcome) == 1 else 0


async def pause(job: Job, *, now: datetime, reason: str) -> int | None:
    """paused_budget with `reason`, counting the pause. The job's pause count
    after this one, or None when it is terminal or gone."""
    client = get_jobs_client()
    count = await _call(
        lambda: client.eval(
            _PAUSE_SCRIPT,
            1,
            job_key(job.tenant, job.job_id),
            JobStatus.PAUSED_BUDGET.value,
            now.isoformat(),
            reason,
            *_TERMINAL_ARGS,
        )
    )
    return int(count) if int(count) > 0 else None


async def store_result(
    tenant: str, job_id: str, result: dict[str, object], *, ttl_seconds: int
) -> None:
    """Hold the job's result apart from the job, for `ttl_seconds`."""
    client = get_jobs_client()
    await _call(
        lambda: client.set(
            result_key(tenant, job_id),
            json.dumps(result, sort_keys=True),
            ex=ttl_seconds,
        )
    )


async def read_result(tenant: str, job_id: str) -> dict[str, object] | None:
    """The held result, or None once it has expired or was never stored."""
    client = get_jobs_client()
    raw = await _call(lambda: client.get(result_key(tenant, job_id)))
    return None if raw is None else json.loads(raw)
