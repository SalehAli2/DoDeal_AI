"""Unit A's three operational state concerns, all on the db2 connection.

  idempotency  Has this exact judgement already been done? Reserved once, per
               (tenant, note, note-text fingerprint).
  rate limit   How many clarification prompts has this USER been sent in the
               window? Keyed on the subject asking, never on the note's author.
  attempts     How many clarification prompts has THIS note already drawn, and
               -- beside the counter, on the same TTL and the same fail-open
               policy -- WHICH note text the first of them was sent about
               (register item 33, the resubmission reference).

THE THREE FAILURE POLICIES ARE NOT THE SAME, and this module is where that is
enforced rather than remembered:

  - Idempotency store unreachable -> DENY. Raises IdempotencyUnavailableError,
    which the route turns into 503 idempotency_unavailable. Failing open here
    would mean doing paid model work twice for one note and, worse, sending a
    salesperson the same clarification question twice. There is no safe
    "probably not a duplicate".
  - Rate-limit store unreachable -> OPEN, log rate_limit_bypassed.
  - Attempt store unreachable    -> OPEN, log attempt_counter_bypassed.
    Both are politeness guards on how often we pester a salesperson. A Redis
    blip must not turn into a 503 on a judgement that is otherwise fine, so the
    read reports "no usage" and the increment is dropped. Every bypass is
    logged at WARNING so it is observable and alertable -- the same shape as
    core/cost/limiter.py's cost_cap_bypassed.

Do not "simplify" these into one policy. They are three different risks.

THE IDEMPOTENCY KEY HAS TWO LIFETIMES (register item 82). It is RESERVED short,
for as long as a judgement may still be running, and CONFIRMED long once the
judgement exists. A worker that is killed outright runs no cleanup, so the short
reservation is what stops it locking a note for a day. Only the reservation
fails closed. The confirm fails OPEN (idempotency_confirm_bypassed): a lost
confirm leaves the short reservation to expire, and a duplicate after that is
judged again, which costs money but is never wrong.

EVERY call here runs inside `operational_breaker` (core/breaker.py). BreakerOpen
is a RedisError, so a refusal takes the same branch: the two politeness guards
bypass, and the reservation still 503s.

ONE LUA SCRIPT: the rate limit's check IS its increment (register item 27), so
two judgements cannot both take the last slot; it runs on fakeredis[lua] in
tests/unit/test_cost_lua.py. The attempt counter stays sequential INCR/EXPIRE,
where a lost race only re-sets the same TTL.

KEY VALUES ARE NEVER LOGGED. The idempotency key embeds a fingerprint of the
note text; logging it would put a stable identifier for a specific note body
into the log stream this service is otherwise careful to keep clean
(ASSUMPTIONS §3.3). Log lines carry the tenant and the request_id and nothing
else. A sentinel test in tests/unit/test_unit_a_state.py enforces this.
"""

from __future__ import annotations

import hashlib
import logging

import redis
from redis import asyncio as redis_async

from dodeal_ai.core.breaker import breaker_field, operational_breaker
from dodeal_ai.core.redis import get_operational_client

_logger = logging.getLogger("dodeal_ai.unit_a.state")

# A reservation only has to EXIST; nothing ever reads its value back. A fixed
# inert byte keeps note-derived content out of the store as well as the log.
_RESERVED = "1"
# What a CONFIRMED key holds: as inert as _RESERVED, and different from it, so a
# Redis console shows which of the two states a key is in. A seam for D3, which
# will store the judgement itself here instead of this marker.
_CONFIRMED = "done"

# TTL sentinels redis returns for a key that has no expiry set and for a key
# that is absent. -1 is the M4 edge: a key that survived without a TTL would
# otherwise pin a user's rate limit or a note's attempt count forever.
_TTL_NO_EXPIRY = -1


class IdempotencyUnavailableError(Exception):
    """The idempotency store could not be reached, so we cannot tell whether
    this judgement has already been done. Fail closed -> 503.

    str() is the fixed reason code alone. No key, no fingerprint, no tenant:
    this exception is expected to reach a log line, and the fingerprint is
    derived from note text (see the module docstring).
    """

    def __init__(self) -> None:
        self.reason_code = "idempotency_unavailable"
        super().__init__(self.reason_code)

    def __str__(self) -> str:
        return self.reason_code


def note_fingerprint(note_text: str) -> str:
    """Hex SHA-256 of the note text as UTF-8, with NO normalisation.

    No normalisation is the point: a note whose whitespace or casing changed is
    a DIFFERENT note as far as the CRM's user is concerned -- they edited it --
    and must be judged again rather than colliding with the previous
    reservation. Normalising here would silently make an edit a duplicate.

    The digest, not the text, goes into the key: a key is a thing that gets
    logged by accident, scanned in a Redis console, and read by whoever is
    debugging. A hash is not reversible to the note body.
    """
    return hashlib.sha256(note_text.encode("utf-8")).hexdigest()


def _idempotency_key(tenant: str, note_id: int, fingerprint: str) -> str:
    return f"idem:{tenant}:judge_note:{note_id}:{fingerprint}"


def _rate_limit_key(tenant: str, subject: str) -> str:
    return f"ratelimit:{tenant}:{subject}"


def _attempt_key(tenant: str, lead_id: int, note_id: int) -> str:
    return f"attempt:{tenant}:{lead_id}:{note_id}"


def _attempt_fingerprint_key(tenant: str, lead_id: int, note_id: int) -> str:
    """Beside the attempt counter, same (tenant, lead, note), same TTL.

    A SECOND KEY rather than a hash holding both. The two are written in the
    same breath and expire on the same TTL, so the only thing a hash would add
    is that they expire on ONE ttl instead of two identical ones -- and it would
    cost the fork of _incr_with_window, which the rate limit and the attempt
    counter share today. See the phase report for the full comparison.
    """
    return f"attempt_fp:{tenant}:{lead_id}:{note_id}"


def _bypass(code: str, tenant: str, request_id: str, exc: BaseException) -> None:
    """One WARNING per bypassed call. Tenant and request_id only -- both are
    identifiers we already log at the gates, neither is note-derived -- plus
    `breaker: open` when the breaker refused rather than the store failing."""
    _logger.warning(
        code,
        extra={
            "reason_code": code,
            "tenant": tenant,
            "request_id": request_id,
            **breaker_field(exc),
        },
    )


async def _incr_with_window(client: redis_async.Redis, key: str, ttl: int) -> int:
    """INCR, then set the window's expiry when the key is NEW or has no TTL.

    The `or` short-circuits, so a brand-new key costs one INCR and one EXPIRE
    and never calls TTL. The TTL == -1 branch is audit finding M4 carried over
    to db2: the cost limiter's Lua sets EXPIRE only on create, so a key that
    somehow exists without a TTL never expires. Here that key would pin a
    note's attempt count forever, silently withholding every future
    clarification prompt. The rate limit's own script carries the same guard.
    """
    count = int(await client.incr(key))
    if count == 1 or int(await client.ttl(key)) == _TTL_NO_EXPIRY:
        await client.expire(key, ttl)
    return count


# --- idempotency: fails CLOSED ---------------------------------------------


async def reserve_idempotency(
    tenant: str,
    note_id: int,
    fingerprint: str,
    *,
    ttl: int,
    request_id: str,
) -> bool:
    """Claim this (tenant, note, fingerprint). True if WE claimed it; False if
    someone already had it -> the route answers 409 duplicate_request.

    SET NX EX in one command, so two concurrent identical requests cannot both
    win: NX is the atomicity here, and it is a single command, so no script is
    needed.

    `ttl` is the SHORT, in-flight lifetime the pipeline derives from its
    deadline; confirm_idempotency applies the long one once the judgement
    exists (the module docstring's two lifetimes).

    Raises IdempotencyUnavailableError on any RedisError -- see the module
    docstring for why this one does not fail open.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        claimed = await operational_breaker().call(
            lambda: get_operational_client().set(key, _RESERVED, nx=True, ex=ttl)
        )
    except redis.RedisError as exc:
        # No key material in the line: the key embeds the fingerprint. A breaker
        # refusal lands here too, and denies for the same reason -- we still
        # cannot tell whether this judgement has already been done.
        _bypass("idempotency_unavailable", tenant, request_id, exc)
        raise IdempotencyUnavailableError() from None
    return bool(claimed)


async def release_idempotency(
    tenant: str,
    note_id: int,
    fingerprint: str,
    *,
    request_id: str,
) -> None:
    """Drop the reservation. BEST EFFORT: swallows every RedisError.

    Called on ANY exit after a successful reservation that did not produce a
    judgement -- a cancellation included -- so that a caller whose judgement
    failed for a reason of ours (the model was down, the backend was down, the
    output would not validate, the deadline passed) can simply retry instead of
    being told 409. If the release itself fails the reservation just expires on
    its short TTL -- annoying, never wrong -- and raising here would replace the
    real error with a less useful one.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        await operational_breaker().call(lambda: get_operational_client().delete(key))
    except redis.RedisError as exc:
        _bypass("idempotency_release_failed", tenant, request_id, exc)


async def confirm_idempotency(
    tenant: str,
    note_id: int,
    fingerprint: str,
    *,
    ttl: int,
    request_id: str,
) -> None:
    """The judgement exists: keep the reservation for the long TTL.

    SET XX EX, so this only ever REPLACES the reservation this request holds and
    never creates one. A key that has already expired stays gone -- writing it
    back would lock the note on the strength of a reservation nobody holds.

    FAILS OPEN, with idempotency_confirm_bypassed. The reservation then expires
    on its short in-flight TTL, and a duplicate arriving after that is judged
    again: money, never correctness. Failing closed here would throw away a
    judgement that is already built and paid for.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        await operational_breaker().call(
            lambda: get_operational_client().set(key, _CONFIRMED, xx=True, ex=ttl)
        )
    except redis.RedisError as exc:
        # No key material in the line, as for the reservation beside it.
        _bypass("idempotency_confirm_bypassed", tenant, request_id, exc)


# --- rate limit: fails OPEN ------------------------------------------------


# Take one slot or refuse, in one execution: the check IS the increment. The
# window is set on creation and on a key with none (audit M4, as in
# _incr_with_window). ARGV[1] is the limit, ARGV[2] the window.
_TAKE_RATE_LIMIT_SCRIPT = """
local count = tonumber(redis.call('GET', KEYS[1]) or '0')
if count >= tonumber(ARGV[1]) then
  return {0, count}
end
local taken = redis.call('INCR', KEYS[1])
if taken == 1 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return {1, count}
"""


async def take_rate_limit(
    tenant: str, subject: str, *, limit: int, ttl: int, request_id: str
) -> tuple[bool, int]:
    """Claim one prompt against this subject's window, in one round trip.

    The only writer of this counter, called only where a prompt would be sent.
    Returns (allowed, count BEFORE this call). Fails OPEN at (True, 0).
    """
    try:
        allowed, count = await operational_breaker().call(
            lambda: get_operational_client().eval(
                _TAKE_RATE_LIMIT_SCRIPT,
                1,
                _rate_limit_key(tenant, subject),
                limit,
                ttl,
            )
        )
    except redis.RedisError as exc:
        _bypass("rate_limit_bypassed", tenant, request_id, exc)
        return True, 0
    return bool(allowed), int(count)


async def read_rate_limit(tenant: str, subject: str, *, request_id: str) -> int:
    """How many clarification prompts this subject has been sent this window.

    ASSUMPTION[Q7]: keyed on the JWT subject -- who is ASKING -- not on the
    note's author_id. The limit exists to stop us pestering one person, and the
    person we would pester is the one making the request.

    READ-ONLY, for a judgement with no question: an exhausted window still
    reports `rate_limited`, and no slot is taken for a prompt nobody receives.

    0 when the store is unreachable (fail open) or the key has expired.
    """
    try:
        raw = await operational_breaker().call(
            lambda: get_operational_client().get(_rate_limit_key(tenant, subject))
        )
    except redis.RedisError as exc:
        _bypass("rate_limit_bypassed", tenant, request_id, exc)
        return 0
    return int(raw or 0)


# --- attempts: fails OPEN --------------------------------------------------


async def read_attempts(
    tenant: str, lead_id: int, note_id: int, *, request_id: str
) -> int:
    """How many clarification prompts this note has already drawn.

    0 when the store is unreachable (fail open) or the key has expired.
    """
    try:
        raw = await operational_breaker().call(
            lambda: get_operational_client().get(_attempt_key(tenant, lead_id, note_id))
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)
        return 0
    return int(raw or 0)


async def increment_attempts(
    tenant: str, lead_id: int, note_id: int, *, ttl: int, request_id: str
) -> None:
    """Count one clarification prompt against this note. Same rule as the rate
    limit: only when a prompt is actually sent."""
    try:
        await operational_breaker().call(
            lambda: _incr_with_window(
                get_operational_client(), _attempt_key(tenant, lead_id, note_id), ttl
            )
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)


# --- the resubmission reference (register item 33): also fails OPEN --------


async def write_attempt_fingerprint(
    tenant: str,
    lead_id: int,
    note_id: int,
    fingerprint: str,
    *,
    ttl: int,
    request_id: str,
) -> None:
    """Record WHICH note text we prompted on, beside the attempt counter.

    Written at the one moment a prompt is actually sent, with the attempt key's
    own TTL, so the reference lives exactly as long as the counter it belongs
    to and dies with it. A resubmission arriving later reads it back and the
    CRM can link the two without this service holding any history.

    SET NX, so this is the FIRST prompted text and stays it. The cap is 1 today
    and a second prompt cannot happen -- but the field is specified as the note
    "as it was first prompted on", and NX makes that true by construction
    instead of by the cap's current value.

    THE VALUE IS A FINGERPRINT, NEVER NOTE TEXT. It is the same digest the
    idempotency key already carries, and it is stored raw here (not inside the
    key) because it is read BACK, which a key name cannot be without a scan.

    Fails OPEN with the attempt counter's own code: this is a reference for a
    later request, and losing it costs a null field, never a judgement.
    """
    try:
        await operational_breaker().call(
            lambda: get_operational_client().set(
                _attempt_fingerprint_key(tenant, lead_id, note_id),
                fingerprint,
                nx=True,
                ex=ttl,
            )
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)


async def read_attempt_fingerprint(
    tenant: str, lead_id: int, note_id: int, *, request_id: str
) -> str | None:
    """The note text we first prompted on for this note, or None.

    None means all of: no prompt was ever sent for this note, the reference
    expired with its counter, or the store is unreachable. The response field
    is documented as "the fingerprint or null" precisely because those collapse
    -- a caller that cannot find a reference does the same thing in all three
    cases, and distinguishing them would leak how long we keep state.
    """
    try:
        raw = await operational_breaker().call(
            lambda: get_operational_client().get(
                _attempt_fingerprint_key(tenant, lead_id, note_id)
            )
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)
        return None
    if raw is None:
        return None
    # decode_responses=True on the operational client (core/redis.py), so redis
    # hands back str; the redis-py stub is typed for both settings. Narrowed the
    # way pipeline.py narrows a ClassifierOutput -- str() would be worse than
    # useless here, since str(b"ab") is "b'ab'" and would ship a fingerprint
    # that matches nothing.
    assert isinstance(raw, str)
    return raw
