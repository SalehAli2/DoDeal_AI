"""Unit B's call jobs in db3 (register item 50): one job per (tenant, job_id),
at most one per (tenant, call_id), and its result held apart from it.

THREE KEYS under the tenant, so a job id from one tenant can never address
another tenant's job, and one index of every running job across tenants:

  call_job:{tenant}:{job_id}          a hash: status, attempts, transcriptions,
                                      pauses, outages, reason, delivery,
                                      request_id, queue, timestamps, metadata
  call_job_by_call:{tenant}:{call_id} the job_id this call was admitted as
  call_result:{tenant}:{job_id}       the stage-1 result, EX result_ttl_seconds
  call_stage2:{tenant}:{job_id}       the stage-2 result, EX result_ttl_seconds
  call_work:{tenant}:{job_id}         a hash of what stage 1 has paid for so
                                      far -- the transcript, each pass's
                                      outcome -- EX result_ttl_seconds
  call_whatsapp:{tenant}:{job_id}:{language}
                                      a regenerated WhatsApp suggestion, EX
                                      no longer than the stage-1 result is
                                      left; its _claim twin, SET NX with a
                                      short EX, is the one request paying
  call_jobs_active                    a sorted set: `<tenant>:<job_id>` for
                                      every job not yet terminal, scored by
                                      its last transition's unix time -- a
                                      paused job by its wake-up time, once
                                      noted
  call_jobs_running                   the same, for the jobs in a running
                                      status only: the sweep reads it first
  call_stage2_pending                 a sorted set: every done job whose
                                      stage 2 is pending, scored when it
                                      became pending or the sweep re-queued it

ONE JOB PER CALL is one Lua script: the index is read and, only if absent, the
index and the job are written together. Two concurrent pushes of one call
therefore make one job, and the second is told the first's job_id.

TERMINAL IS FINAL. done, failed and dead_letter refuse every later transition,
claim and pause inside the same scripts, and on reaching one the job hash and
its index take the tenant's result_ttl_seconds, so a finished job and its
dedupe entry expire together, and it leaves the active set.

NOTHING LIVES FOREVER. Every other move -- the push, a claim, a pause, a
transcription's start, a transition -- gives the job and its index
CALL_JOB_RECORD_TTL_SECONDS from that moment, in the same script, and scores
the job in the active set at that moment; a pause then re-scores it at its
wake-up time (note_wake). The sweep (sweep.py in the unit) reads that set for
jobs left too long past their score; one it re-enqueues is marked with the
transition it found, so it is re-enqueued once per stall.

DELIVERY IS APART FROM STATUS (register item 50). A job whose result exists is
`done` whatever becomes of its callback; `delivery` says that: pending while
the event is owed, then delivered or delivery_failed, and it moves only out of
pending. No callback URL, no delivery: the field stays empty.

STAGE 2 IS APART FROM STATUS TOO. A `done` job stays done while wave 2 runs;
`stage2` says where that is: not_eligible, or pending until the stage-2 task
settles it done or failed, with the reason in `stage2_reason`. It is set with
the move to done, in the same script, and moves only out of pending. A
pending stage 2 sits in call_stage2_pending until it settles, so the sweep
can find one whose run was lost; its callback, call.stage2 or call.failed for
stage 2, has a delivery field of its own, `stage2_delivery`. So has each
translation's call.translation, one per target (`translation_delivery:ar`).

FAILS CLOSED. The job store is the job: a store that cannot be read or written
raises JobStoreUnavailable (a 503 at the route, a retry in the worker), never a
guess. Every call runs inside the jobs breaker and catches RedisError.

`metadata` holds the push body's fields, the signed audio link among them. It
is never logged, never on a repr, and never in an exception.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

import redis

from dodeal_ai.core.breaker import jobs_breaker
from dodeal_ai.core.config import get_settings
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


class DeliveryState(StrEnum):
    """Where a job's one callback event is; read on GET beside the status."""

    PENDING = "pending"
    DELIVERED = "delivered"
    DELIVERY_FAILED = "delivery_failed"


class Stage2State(StrEnum):
    """Where a done job's wave 2 is; read on GET beside the status."""

    NOT_ELIGIBLE = "not_eligible"
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


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
    # Transcriptions started, a paid call each; item 35 allows two.
    transcriptions: int
    pauses: int
    # The pauses a cost-store outage caused: what the resume's backoff doubles on.
    outages: int
    # Re-enqueues by the stuck-job sweep, each under an arq id of its own.
    sweeps: int
    # Starts per analysis pass, a paid call each; a pass may start twice.
    passes: dict[str, int]
    request_id: str
    reason: str | None
    # The callback's state; None when the tenant has no callback to send to.
    delivery: DeliveryState | None
    # Wave 2's state once the job is done, and why it failed; None before.
    stage2: Stage2State | None
    stage2_reason: str | None
    queue: str
    created_at: str
    updated_at: str
    finished_at: str | None
    metadata: dict[str, object] = field(repr=False)
    # Re-queues of a lost stage 2 by the sweep: one, then stage 2 has failed.
    stage2_sweeps: int = 0
    # Stage 2's own callback; None while none is owed.
    stage2_delivery: DeliveryState | None = None
    # The index key the job was admitted under, when not its call's own: a
    # re-analysis job's (reanalysis_index_key), so it never touches the call's.
    index: str | None = None
    # Each translation's own callback, by target; absent while none is owed.
    translation_delivery: dict[str, DeliveryState] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


def job_key(tenant: str, job_id: str) -> str:
    return f"call_job:{tenant}:{job_id}"


def call_index_key(tenant: str, call_id: int) -> str:
    return f"call_job_by_call:{tenant}:{call_id}"


def reanalysis_index_key(tenant: str, digest: str) -> str:
    """A re-analysis's index: one job per call, stages and versions."""
    return f"call_job_by_reanalysis:{tenant}:{digest}"


def result_key(tenant: str, job_id: str) -> str:
    return f"call_result:{tenant}:{job_id}"


def stage2_result_key(tenant: str, job_id: str) -> str:
    return f"call_stage2:{tenant}:{job_id}"


def translation_key(tenant: str, job_id: str, target: str) -> str:
    return f"call_translation:{tenant}:{job_id}:{target}"


def whatsapp_key(tenant: str, job_id: str, language: str) -> str:
    return f"call_whatsapp:{tenant}:{job_id}:{language}"


def whatsapp_claim_key(tenant: str, job_id: str, language: str) -> str:
    return f"call_whatsapp_claim:{tenant}:{job_id}:{language}"


def work_key(tenant: str, job_id: str) -> str:
    return f"call_work:{tenant}:{job_id}"


# A pass's start count lives in the job hash under this prefix and its name.
_PASS_FIELD = "pass:"
# A translation's delivery lives under this prefix and its target.
_TRANSLATION_DELIVERY_FIELD = "translation_delivery:"


# Every job not yet terminal, across tenants, scored by its last transition.
ACTIVE_JOBS_KEY = "call_jobs_active"

# The part of the active set a run holds, scored alike: what the sweep reads
# first, so a backlog of waiting jobs never hides one whose run died.
RUNNING_JOBS_KEY = "call_jobs_running"

# Every done job whose stage 2 is pending, scored when it became pending.
STAGE2_PENDING_KEY = "call_stage2_pending"

# The statuses a run holds a job in; a job left in one has lost its run.
RUNNING_STATUSES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.DOWNLOADING,
        JobStatus.TRANSCRIBING,
        JobStatus.ANALYSING,
        JobStatus.DELIVERING,
    }
)


def active_member(tenant: str, job_id: str) -> str:
    """The job's member in the active set. A tenant is one DNS label, so the
    first colon splits it back."""
    return f"{tenant}:{job_id}"


# Prepended to every script that moves a job still running. KEYS[1] the job,
# KEYS[2] its call index, KEYS[3] the active set, KEYS[4] the running set:
# both keys expire `ttl` from now and the job is scored at `score`, this
# move's time, in the running set too while its status is a running one.
_RUNNING_TABLE = ", ".join(
    f"{status.value} = true" for status in sorted(RUNNING_STATUSES)
)
_TOUCH = f"""
local RUNNING = {{{_RUNNING_TABLE}}}
local function touch(ttl, score, member)
  redis.call('EXPIRE', KEYS[1], ttl)
  redis.call('EXPIRE', KEYS[2], ttl)
  redis.call('ZADD', KEYS[3], score, member)
  if RUNNING[redis.call('HGET', KEYS[1], 'status')] then
    redis.call('ZADD', KEYS[4], score, member)
  else
    redis.call('ZREM', KEYS[4], member)
  end
end
"""


# KEYS: the new job, the call index, the active set, the running set. ARGV:
# the new job_id, the record TTL, now's score, the member, then field/value
# pairs. Returns {1, job_id} when this call made the job, {0, the existing
# job_id} when the call already had one; nothing is written then.
_CREATE_SCRIPT = (
    _TOUCH
    + """
local existing = redis.call('GET', KEYS[2])
if existing then
  return {0, existing}
end
redis.call('SET', KEYS[2], ARGV[1])
for i = 5, #ARGV, 2 do
  redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
end
touch(ARGV[2], ARGV[3], ARGV[4])
return {1, ARGV[1]}
"""
)

# KEYS: the job, the call index, the active set, the running set, the stage-2
# pending set. ARGV: status, now, reason, the result TTL, "1" when the status
# is terminal, the delivery state or "" to leave it, the stage-2 state or ""
# to leave it, the record TTL, now's score, the member, then every terminal
# status. 0 missing, -1 already terminal, 1 moved. A terminal move sets
# finished_at and the result TTL on both keys and leaves the active and
# running sets; any other is touched. A stage 2 made pending joins the pending
# set at now's score.
_TRANSITION_SCRIPT = (
    _TOUCH
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 11, #ARGV do
  if current == ARGV[i] then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'status', ARGV[1], 'updated_at', ARGV[2], 'reason', ARGV[3])
if ARGV[6] ~= '' then
  redis.call('HSET', KEYS[1], 'delivery', ARGV[6])
end
if ARGV[7] ~= '' then
  redis.call('HSET', KEYS[1], 'stage2', ARGV[7])
  if ARGV[7] == 'pending' then
    redis.call('ZADD', KEYS[5], ARGV[9], ARGV[10])
  end
end
if ARGV[5] == '1' then
  redis.call('HSET', KEYS[1], 'finished_at', ARGV[2])
  redis.call('EXPIRE', KEYS[1], ARGV[4])
  redis.call('EXPIRE', KEYS[2], ARGV[4])
  redis.call('ZREM', KEYS[3], ARGV[10])
  redis.call('ZREM', KEYS[4], ARGV[10])
else
  touch(ARGV[8], ARGV[9], ARGV[10])
end
return 1
"""
)

# KEYS: the job, the call index, the active set, the running set. ARGV: max
# tries, now, the running status, the record TTL, now's score, the member, then
# every terminal status. {-2, 0} missing, {-1, 0} terminal, {0, attempts} at
# the cap (nothing moves), {1, attempts} claimed: attempts incremented, the
# reason cleared.
_CLAIM_SCRIPT = (
    _TOUCH
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {-2, 0}
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 7, #ARGV do
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
touch(ARGV[4], ARGV[5], ARGV[6])
return {1, attempts}
"""
)

# KEYS: the job, the call index, the active set, the running set. ARGV: the
# paused status, now, reason, "1" to give back the attempt this run claimed,
# "1" when the cost store was down, the record TTL, now's score, the member,
# then every terminal status. {0, 0} missing, {-1, 0} terminal, else the job's
# pause and outage counts after this one. A pause never costs an attempt
# (core(105)).
_PAUSE_SCRIPT = (
    _TOUCH
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return {0, 0}
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 9, #ARGV do
  if current == ARGV[i] then
    return {-1, 0}
  end
end
redis.call('HSET', KEYS[1], 'status', ARGV[1], 'updated_at', ARGV[2], 'reason', ARGV[3])
touch(ARGV[6], ARGV[7], ARGV[8])
if ARGV[4] == '1' and tonumber(redis.call('HGET', KEYS[1], 'attempts') or '0') > 0 then
  redis.call('HINCRBY', KEYS[1], 'attempts', -1)
end
local outages = tonumber(redis.call('HGET', KEYS[1], 'outages') or '0')
if ARGV[5] == '1' then
  outages = redis.call('HINCRBY', KEYS[1], 'outages', 1)
end
return {redis.call('HINCRBY', KEYS[1], 'pauses', 1), outages}
"""
)

# KEYS: the job, the call index, the active set, the running set. ARGV: now,
# the transcribing status, the record TTL, now's score, the member, then every
# terminal status. 0 missing, -1 terminal, else the job's transcriptions after
# this one: `transcribing` and counted in one step, before the paid call.
_TRANSCRIBE_SCRIPT = (
    _TOUCH
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 6, #ARGV do
  if current == ARGV[i] then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'status', ARGV[2], 'updated_at', ARGV[1], 'reason', '')
touch(ARGV[3], ARGV[4], ARGV[5])
return redis.call('HINCRBY', KEYS[1], 'transcriptions', 1)
"""
)

# KEYS: the job. ARGV: the settled state, now, the delivery's field. 0
# missing, -1 not pending (settled already, or never owed), 1 settled. The
# status is never touched.
_SETTLE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('HGET', KEYS[1], ARGV[3]) ~= 'pending' then
  return -1
end
redis.call('HSET', KEYS[1], ARGV[3], ARGV[1], 'updated_at', ARGV[2])
return 1
"""

# KEYS: the job. ARGV: the delivery's field, now. 0 missing, 1 owed: pending
# again even when an earlier one settled, for a new translation to send.
_OWE_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
redis.call('HSET', KEYS[1], ARGV[1], 'pending', 'updated_at', ARGV[2])
return 1
"""

# KEYS: the job, the stage-2 pending set. ARGV: the settled stage-2 state, its
# reason or "", now, the member, stage 2's delivery state or "" for none owed.
# 0 missing, -1 not pending (settled already, or never owed), 1 settled. The
# status is never touched, and the job leaves the pending set either way.
_STAGE2_SCRIPT = """
redis.call('ZREM', KEYS[2], ARGV[4])
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('HGET', KEYS[1], 'stage2') ~= 'pending' then
  return -1
end
redis.call('HSET', KEYS[1], 'stage2', ARGV[1], 'stage2_reason', ARGV[2], 'updated_at', ARGV[3])
if ARGV[5] ~= '' then
  redis.call('HSET', KEYS[1], 'stage2_delivery', ARGV[5])
end
return 1
"""

# KEYS: the job, the stage-2 pending set. ARGV: the member, now's score, the
# seconds a pending stage 2 may wait. A job gone or no longer pending leaves
# the set. -1 for that, or for one not yet past its wait; else how often the
# sweep has re-queued it.
_STAGE2_STUCK_SCRIPT = """
if redis.call('HGET', KEYS[1], 'stage2') ~= 'pending' then
  redis.call('ZREM', KEYS[2], ARGV[1])
  return -1
end
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) - tonumber(ARGV[3]) then
  return -1
end
return tonumber(redis.call('HGET', KEYS[1], 'stage2_sweeps') or '0')
"""

# KEYS: the job, the stage-2 pending set. ARGV: the member, now's score. A
# pending stage 2 the sweep re-queued: counted, and its wait counted from now.
# 0 when it is gone or no longer pending, else its re-queues after this one.
_STAGE2_REQUEUED_SCRIPT = """
if redis.call('HGET', KEYS[1], 'stage2') ~= 'pending' then
  return 0
end
redis.call('ZADD', KEYS[2], 'XX', ARGV[2], ARGV[1])
return redis.call('HINCRBY', KEYS[1], 'stage2_sweeps', 1)
"""

# KEYS: the job, the call index, the active set, the running set. ARGV: now,
# the pass's field, the record TTL, now's score, the member, then every
# terminal status. 0 missing, -1 terminal, else the pass's starts after this
# one: counted before the paid call is made, the status left as it is.
_PASS_SCRIPT = (
    _TOUCH
    + """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
local current = redis.call('HGET', KEYS[1], 'status')
for i = 6, #ARGV do
  if current == ARGV[i] then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'updated_at', ARGV[1])
touch(ARGV[3], ARGV[4], ARGV[5])
return redis.call('HINCRBY', KEYS[1], ARGV[2], 1)
"""
)

# KEYS: the job. ARGV: the pass's field. 0 missing, -1 stage 2 not pending,
# else the pass's starts after this one: a stage-2 pass counted on a done job,
# before its paid call, nothing else about the job touched.
_STAGE2_PASS_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('HGET', KEYS[1], 'stage2') ~= 'pending' then
  return -1
end
return redis.call('HINCRBY', KEYS[1], ARGV[1], 1)
"""

# KEYS: the work hash. ARGV: the field, its JSON, the TTL. One step, so the
# hash never holds a field without its expiry.
_WORK_SCRIPT = """
redis.call('HSET', KEYS[1], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[3])
return 1
"""

# KEYS: the job, the active set, the running set. ARGV: the member, now's
# score, then pairs of a status the sweep may re-enqueue and the seconds past
# its score it waits. A job gone leaves both sets: {0}. One in another status,
# not yet past its wait, or swept at its last transition already: {-1}. Else
# it is marked swept at that transition: {1, its sweeps so far, its queue, its
# status, its pauses}; a running one is re-scored at now in the running set,
# so it waits there for its re-run instead of filling the next sweep's batch.
_SWEEP_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  redis.call('ZREM', KEYS[2], ARGV[1])
  redis.call('ZREM', KEYS[3], ARGV[1])
  return {0}
end
local current = redis.call('HGET', KEYS[1], 'status')
local wait = nil
for i = 3, #ARGV, 2 do
  if current == ARGV[i] then
    wait = tonumber(ARGV[i + 1])
  end
end
local score = redis.call('ZSCORE', KEYS[2], ARGV[1])
local moved = redis.call('HGET', KEYS[1], 'updated_at')
if wait == nil or not score or tonumber(score) > tonumber(ARGV[2]) - wait then
  return {-1}
end
if redis.call('HGET', KEYS[1], 'swept') == moved then
  return {-1}
end
redis.call('HSET', KEYS[1], 'swept', moved)
redis.call('ZADD', KEYS[3], 'XX', ARGV[2], ARGV[1])
local sweeps = redis.call('HINCRBY', KEYS[1], 'sweeps', 1)
local pauses = redis.call('HGET', KEYS[1], 'pauses') or '0'
return {1, sweeps, redis.call('HGET', KEYS[1], 'queue'), current, pauses}
"""

# KEYS: the job. ARGV: "1" to take back the sweep count too. The mark goes, so
# the next sweep may re-enqueue the job; nothing is written to a job gone.
_UNMARK_SCRIPT = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
redis.call('HDEL', KEYS[1], 'swept')
if ARGV[1] == '1' then
  redis.call('HINCRBY', KEYS[1], 'sweeps', -1)
end
return 1
"""

# KEYS: the job, the active set. ARGV: the paused status, the wake-up score,
# the member. A job still paused is scored at its wake-up, so the sweep counts
# its wait from then; 0 when it is gone or has moved on.
_WAKE_SCRIPT = """
if redis.call('HGET', KEYS[1], 'status') ~= ARGV[1] then
  return 0
end
redis.call('ZADD', KEYS[2], 'XX', ARGV[2], ARGV[3])
return 1
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


def _keys(job: Job) -> tuple[str, str, str, str]:
    """A moving script's four keys: the job, its call index, the active set,
    the running set."""
    return (
        job_key(job.tenant, job.job_id),
        job.index or call_index_key(job.tenant, job.call_id),
        ACTIVE_JOBS_KEY,
        RUNNING_JOBS_KEY,
    )


def _touch_args(tenant: str, job_id: str, now: datetime) -> tuple[int, float, str]:
    """The record TTL, this move's score and the member, as `touch` takes them."""
    ttl = get_settings().call_job_record_ttl_seconds
    return ttl, now.timestamp(), active_member(tenant, job_id)


async def create_job(
    tenant: str,
    call_id: int,
    *,
    job_id: str,
    request_id: str,
    queue: str,
    metadata: dict[str, object],
    now: datetime,
    index: str | None = None,
) -> tuple[str, bool]:
    """Admit `call_id` as `job_id`, queued, unless the call already has a job
    -- or, with `index`, unless that index already names one. Returns (the
    job_id, whether this call made it)."""
    stamp = now.isoformat()
    fields = {
        "tenant": tenant,
        "job_id": job_id,
        "call_id": str(call_id),
        "status": JobStatus.QUEUED.value,
        "attempts": "0",
        "transcriptions": "0",
        "pauses": "0",
        "outages": "0",
        "request_id": request_id,
        "reason": "",
        "delivery": "",
        "queue": queue,
        "created_at": stamp,
        "updated_at": stamp,
        "finished_at": "",
        "metadata": json.dumps(metadata, sort_keys=True),
        **({} if index is None else {"index": index}),
    }
    pairs = [item for pair in fields.items() for item in pair]
    client = get_jobs_client()
    made, existing = await _call(
        lambda: client.eval(
            _CREATE_SCRIPT,
            4,
            job_key(tenant, job_id),
            index or call_index_key(tenant, call_id),
            ACTIVE_JOBS_KEY,
            RUNNING_JOBS_KEY,
            job_id,
            *_touch_args(tenant, job_id, now),
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
        # A job stored before the count existed has started none.
        transcriptions=int(raw.get("transcriptions", "0")),
        pauses=int(raw["pauses"]),
        # A job stored before the outage count existed has had none.
        outages=int(raw.get("outages", "0")),
        sweeps=int(raw.get("sweeps", "0")),
        passes={
            name.removeprefix(_PASS_FIELD): int(value)
            for name, value in raw.items()
            if name.startswith(_PASS_FIELD)
        },
        request_id=raw["request_id"],
        reason=raw["reason"] or None,
        delivery=DeliveryState(raw["delivery"]) if raw.get("delivery") else None,
        stage2=Stage2State(raw["stage2"]) if raw.get("stage2") else None,
        stage2_reason=raw.get("stage2_reason") or None,
        stage2_sweeps=int(raw.get("stage2_sweeps", "0")),
        stage2_delivery=(
            DeliveryState(raw["stage2_delivery"])
            if raw.get("stage2_delivery")
            else None
        ),
        queue=raw["queue"],
        created_at=raw["created_at"],
        updated_at=raw["updated_at"],
        finished_at=raw["finished_at"] or None,
        metadata=json.loads(raw["metadata"]),
        index=raw.get("index") or None,
        translation_delivery={
            name.removeprefix(_TRANSLATION_DELIVERY_FIELD): DeliveryState(value)
            for name, value in raw.items()
            if name.startswith(_TRANSLATION_DELIVERY_FIELD)
        },
    )


async def transition(
    job: Job,
    status: JobStatus,
    *,
    now: datetime,
    reason: str | None = None,
    ttl_seconds: int,
    delivery: DeliveryState | None = None,
    stage2: Stage2State | None = None,
) -> bool:
    """Move a job that is not terminal to `status`, and its delivery and its
    stage-2 state with it when given. A terminal status sets finished_at and
    `ttl_seconds` on the job and its call index; any other gives both the
    record TTL. False when the job is gone or already terminal."""
    client = get_jobs_client()
    moved = await _call(
        lambda: client.eval(
            _TRANSITION_SCRIPT,
            5,
            *_keys(job),
            STAGE2_PENDING_KEY,
            status.value,
            now.isoformat(),
            reason or "",
            ttl_seconds,
            "1" if status in TERMINAL else "0",
            "" if delivery is None else delivery.value,
            "" if stage2 is None else stage2.value,
            *_touch_args(job.tenant, job.job_id, now),
            *_TERMINAL_ARGS,
        )
    )
    return int(moved) == 1


async def settle_stage2(
    job: Job,
    state: Stage2State,
    *,
    now: datetime,
    reason: str | None = None,
    delivery: DeliveryState | None = None,
) -> bool:
    """A pending stage 2 becomes `state`, with its reason and, when given, its
    own delivery owed, whatever the job's status. False when it was not
    pending or the job is gone."""
    client = get_jobs_client()
    settled = await _call(
        lambda: client.eval(
            _STAGE2_SCRIPT,
            2,
            job_key(job.tenant, job.job_id),
            STAGE2_PENDING_KEY,
            state.value,
            reason or "",
            now.isoformat(),
            active_member(job.tenant, job.job_id),
            "" if delivery is None else delivery.value,
        )
    )
    return int(settled) == 1


# The field each delivery lives in: stage 1's (or a failed job's), stage 2's.
DELIVERY_FIELD = "delivery"
STAGE2_DELIVERY_FIELD = "stage2_delivery"


def translation_delivery_field(target: str) -> str:
    """The field the translation into `target` has its delivery in."""
    return f"{_TRANSLATION_DELIVERY_FIELD}{target}"


async def settle_delivery(
    job: Job,
    state: DeliveryState,
    *,
    now: datetime,
    stage2: bool = False,
    delivery_field: str | None = None,
) -> bool:
    """A pending delivery -- `delivery_field`'s when given, else stage 2's own
    when `stage2`, else the job's -- becomes `state`, whatever the job's
    status. False when it was not pending -- settled already, or never owed --
    or is gone."""
    client = get_jobs_client()
    named = delivery_field or (STAGE2_DELIVERY_FIELD if stage2 else DELIVERY_FIELD)
    settled = await _call(
        lambda: client.eval(
            _SETTLE_SCRIPT,
            1,
            job_key(job.tenant, job.job_id),
            state.value,
            now.isoformat(),
            named,
        )
    )
    return int(settled) == 1


async def owe_translation_delivery(job: Job, target: str, *, now: datetime) -> bool:
    """The translation into `target` owed to the callback: its delivery
    pending, whatever came of an earlier one. False when the job is gone."""
    client = get_jobs_client()
    owed = await _call(
        lambda: client.eval(
            _OWE_SCRIPT,
            1,
            job_key(job.tenant, job.job_id),
            translation_delivery_field(target),
            now.isoformat(),
        )
    )
    return int(owed) == 1


async def claim_attempt(
    job: Job, *, max_tries: int, now: datetime, status: JobStatus
) -> int | None:
    """Start one processing attempt: attempts + 1 and `status`, atomically,
    unless the job is terminal or gone (None) or at `max_tries` (0)."""
    client = get_jobs_client()
    outcome, attempts = await _call(
        lambda: client.eval(
            _CLAIM_SCRIPT,
            4,
            *_keys(job),
            max_tries,
            now.isoformat(),
            status.value,
            *_touch_args(job.tenant, job.job_id, now),
            *_TERMINAL_ARGS,
        )
    )
    if int(outcome) < 0:
        return None
    return int(attempts) if int(outcome) == 1 else 0


async def start_transcription(job: Job, *, now: datetime) -> int | None:
    """`transcribing`, counting one more transcription, before the paid call.
    The job's transcriptions after this one, or None when terminal or gone."""
    client = get_jobs_client()
    count = await _call(
        lambda: client.eval(
            _TRANSCRIBE_SCRIPT,
            4,
            *_keys(job),
            now.isoformat(),
            JobStatus.TRANSCRIBING.value,
            *_touch_args(job.tenant, job.job_id, now),
            *_TERMINAL_ARGS,
        )
    )
    return int(count) if int(count) > 0 else None


async def start_pass(job: Job, name: str, *, now: datetime) -> int | None:
    """Count one more start of analysis pass `name`, before its paid call.
    The pass's starts after this one, or None when the job is terminal or gone."""
    client = get_jobs_client()
    count = await _call(
        lambda: client.eval(
            _PASS_SCRIPT,
            4,
            *_keys(job),
            now.isoformat(),
            f"{_PASS_FIELD}{name}",
            *_touch_args(job.tenant, job.job_id, now),
            *_TERMINAL_ARGS,
        )
    )
    return int(count) if int(count) > 0 else None


async def start_stage2_pass(job: Job, name: str, *, now: datetime) -> int | None:
    """Count one more start of stage-2 pass `name` on a done job, before its
    paid call. The pass's starts after this one, or None when the job is gone
    or its stage 2 is no longer pending. `now` is taken for the Starter shape."""
    client = get_jobs_client()
    count = await _call(
        lambda: client.eval(
            _STAGE2_PASS_SCRIPT,
            1,
            job_key(job.tenant, job.job_id),
            f"{_PASS_FIELD}{name}",
        )
    )
    return int(count) if int(count) > 0 else None


async def pause(
    job: Job,
    *,
    now: datetime,
    reason: str,
    claimed: bool = False,
    outage: bool = False,
) -> tuple[int, int] | None:
    """paused_budget with `reason`, counting the pause, and an outage when the
    cost store was down. `claimed` gives back the attempt this run took. The
    job's (pauses, outages) after this one, or None when terminal or gone."""
    client = get_jobs_client()
    pauses, outages = await _call(
        lambda: client.eval(
            _PAUSE_SCRIPT,
            4,
            *_keys(job),
            JobStatus.PAUSED_BUDGET.value,
            now.isoformat(),
            reason,
            "1" if claimed else "0",
            "1" if outage else "0",
            *_touch_args(job.tenant, job.job_id, now),
            *_TERMINAL_ARGS,
        )
    )
    return (int(pauses), int(outages)) if int(pauses) > 0 else None


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
    return await _read_json(result_key(tenant, job_id))


async def store_stage2_result(
    tenant: str, job_id: str, result: dict[str, object], *, ttl_seconds: int
) -> None:
    """Hold the job's stage-2 result beside its stage-1 result, for
    `ttl_seconds`."""
    client = get_jobs_client()
    await _call(
        lambda: client.set(
            stage2_result_key(tenant, job_id),
            json.dumps(result, sort_keys=True),
            ex=ttl_seconds,
        )
    )


async def read_stage2_result(tenant: str, job_id: str) -> dict[str, object] | None:
    """The held stage-2 result, or None once it has expired or before it is."""
    return await _read_json(stage2_result_key(tenant, job_id))


async def store_translation(
    tenant: str,
    job_id: str,
    target: str,
    result: dict[str, object],
    *,
    ttl_seconds: int,
) -> None:
    """Hold a call's translation into `target` for `ttl_seconds`."""
    client = get_jobs_client()
    await _call(
        lambda: client.set(
            translation_key(tenant, job_id, target),
            json.dumps(result, sort_keys=True),
            ex=ttl_seconds,
        )
    )


async def read_translation(
    tenant: str, job_id: str, target: str
) -> dict[str, object] | None:
    """The held translation into `target`, or None."""
    return await _read_json(translation_key(tenant, job_id, target))


async def result_seconds_left(tenant: str, job_id: str) -> int | None:
    """Whole seconds before the stage-1 result expires; None when it is gone
    or holds no expiry."""
    client = get_jobs_client()
    left = await _call(lambda: client.ttl(result_key(tenant, job_id)))
    return int(left) if int(left) > 0 else None


async def claim_whatsapp(
    tenant: str, job_id: str, language: str, *, ttl_seconds: int
) -> bool:
    """The one request that may pay for this language's suggestion: True for
    the first, False while another holds the claim."""
    client = get_jobs_client()
    key = whatsapp_claim_key(tenant, job_id, language)
    taken = await _call(lambda: client.set(key, "1", nx=True, ex=ttl_seconds))
    return bool(taken)


async def release_whatsapp(tenant: str, job_id: str, language: str) -> None:
    """Give the claim back, so a later request may ask again."""
    client = get_jobs_client()
    await _call(lambda: client.delete(whatsapp_claim_key(tenant, job_id, language)))


async def store_whatsapp(
    tenant: str,
    job_id: str,
    language: str,
    suggestion: dict[str, object],
    *,
    ttl_seconds: int,
) -> None:
    """Hold a regenerated suggestion in `language` for `ttl_seconds`."""
    client = get_jobs_client()
    await _call(
        lambda: client.set(
            whatsapp_key(tenant, job_id, language),
            json.dumps(suggestion, sort_keys=True),
            ex=ttl_seconds,
        )
    )


async def read_whatsapps(
    tenant: str, job_id: str, languages: Sequence[str]
) -> dict[str, object]:
    """Every held suggestion of the job among `languages`, by language, in
    one read."""
    client = get_jobs_client()
    keys = [whatsapp_key(tenant, job_id, language) for language in languages]
    raws = await _call(lambda: client.mget(keys))
    return {
        language: json.loads(raw)
        for language, raw in zip(languages, raws, strict=True)
        if raw is not None
    }


async def _read_json(key: str) -> dict[str, object] | None:
    client = get_jobs_client()
    raw = await _call(lambda: client.get(key))
    return None if raw is None else json.loads(raw)


async def stale_jobs(before: datetime, *, limit: int) -> list[tuple[str, str]]:
    """Up to `limit` (tenant, job_id) pairs whose last move was before
    `before`, oldest first."""
    return await _stale(ACTIVE_JOBS_KEY, before, limit)


async def stale_running(before: datetime, *, limit: int) -> list[tuple[str, str]]:
    """Up to `limit` (tenant, job_id) pairs in a running status whose last
    move was before `before`, oldest first."""
    return await _stale(RUNNING_JOBS_KEY, before, limit)


async def stale_stage2(before: datetime, *, limit: int) -> list[tuple[str, str]]:
    """Up to `limit` (tenant, job_id) pairs whose stage 2 has been pending
    since before `before`, oldest first."""
    return await _stale(STAGE2_PENDING_KEY, before, limit)


async def _stale(key: str, before: datetime, limit: int) -> list[tuple[str, str]]:
    """Up to `limit` members of the sorted set `key` scored before `before`."""
    client = get_jobs_client()
    members = await _call(
        lambda: client.zrangebyscore(
            key, "-inf", before.timestamp(), start=0, num=limit
        )
    )
    # Plain members: no scores were asked for.
    pairs = (
        _text(member).split(":", 1)
        for member in members
        if isinstance(member, str | bytes)
    )
    return [(tenant, job_id) for tenant, job_id in pairs]


@dataclass(frozen=True, slots=True)
class Swept:
    """A job the sweep marked: its sweeps so far (this one counted), its queue,
    the status it was left in, and its pauses."""

    sweeps: int
    queue: str
    status: JobStatus
    pauses: int


async def mark_swept(
    tenant: str,
    job_id: str,
    *,
    now: datetime,
    waits: Mapping[JobStatus, int],
) -> Swept | None:
    """Mark a job as swept at its last transition when its status is one of
    `waits` and it is that many seconds past its score at `now`. None when it
    is gone (it then leaves the active set), in another status, not yet past
    its wait, or swept at this transition already."""
    client = get_jobs_client()
    pairs = [str(item) for status in sorted(waits) for item in (status, waits[status])]
    marked = await _call(
        lambda: client.eval(
            _SWEEP_SCRIPT,
            3,
            job_key(tenant, job_id),
            ACTIVE_JOBS_KEY,
            RUNNING_JOBS_KEY,
            active_member(tenant, job_id),
            now.timestamp(),
            *pairs,
        )
    )
    if int(marked[0]) != 1:
        return None
    return Swept(
        sweeps=int(marked[1]),
        queue=_text(marked[2]),
        status=JobStatus(_text(marked[3])),
        pauses=int(marked[4]),
    )


async def unmark_swept(tenant: str, job_id: str, *, uncount: bool = False) -> None:
    """Forget a sweep's mark, so the next sweep may re-enqueue the job;
    `uncount` takes its sweep back too, when nothing was re-enqueued."""
    client = get_jobs_client()
    await _call(
        lambda: client.eval(
            _UNMARK_SCRIPT, 1, job_key(tenant, job_id), "1" if uncount else "0"
        )
    )


async def stage2_stuck(
    tenant: str, job_id: str, *, now: datetime, wait_seconds: int
) -> int | None:
    """How often the sweep has re-queued this job's stage 2, once it has been
    pending `wait_seconds` past its score at `now`; None when it has not, or is
    no longer pending (it then leaves the pending set)."""
    client = get_jobs_client()
    sweeps = await _call(
        lambda: client.eval(
            _STAGE2_STUCK_SCRIPT,
            2,
            job_key(tenant, job_id),
            STAGE2_PENDING_KEY,
            active_member(tenant, job_id),
            now.timestamp(),
            wait_seconds,
        )
    )
    return None if int(sweeps) < 0 else int(sweeps)


async def mark_stage2_requeued(tenant: str, job_id: str, *, now: datetime) -> None:
    """Count a re-queue of a pending stage 2, and count its wait from `now`.
    A stage 2 no longer pending is left as it is."""
    client = get_jobs_client()
    await _call(
        lambda: client.eval(
            _STAGE2_REQUEUED_SCRIPT,
            2,
            job_key(tenant, job_id),
            STAGE2_PENDING_KEY,
            active_member(tenant, job_id),
            now.timestamp(),
        )
    )


async def note_wake(job: Job, *, wake_at: datetime) -> bool:
    """Score a paused job at its wake-up, so the sweep counts from then. False
    when it is gone or no longer paused."""
    client = get_jobs_client()
    noted = await _call(
        lambda: client.eval(
            _WAKE_SCRIPT,
            2,
            job_key(job.tenant, job.job_id),
            ACTIVE_JOBS_KEY,
            JobStatus.PAUSED_BUDGET.value,
            wake_at.timestamp(),
            active_member(job.tenant, job.job_id),
        )
    )
    return int(noted) == 1


async def store_work(
    tenant: str,
    job_id: str,
    field: str,
    value: dict[str, object],
    *,
    ttl_seconds: int,
) -> None:
    """Keep one piece of stage 1's paid work -- the transcript, or a pass's
    outcome -- for `ttl_seconds`, so a later run never pays for it again."""
    client = get_jobs_client()
    await _call(
        lambda: client.eval(
            _WORK_SCRIPT,
            1,
            work_key(tenant, job_id),
            field,
            json.dumps(value, sort_keys=True),
            ttl_seconds,
        )
    )


async def read_work(tenant: str, job_id: str) -> dict[str, dict[str, object]]:
    """Every piece of work kept for the job, by field; {} when none is."""
    client = get_jobs_client()
    stored = await _call(lambda: client.hgetall(work_key(tenant, job_id)))
    return {str(name): json.loads(_text(value)) for name, value in stored.items()}


async def clear_work(tenant: str, job_id: str) -> None:
    """Drop the work once the stage-1 result holds it all."""
    client = get_jobs_client()
    await _call(lambda: client.delete(work_key(tenant, job_id)))
