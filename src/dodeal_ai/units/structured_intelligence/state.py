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

A CONFIRMED KEY HOLDS THE VALIDATED JUDGEMENT (register items 1 and 2), as JSON,
so a duplicate is answered from it with no model call. Reading it back fails
closed like the reservation; a value that no longer validates is taken over by
one script and judged again.

EVERY call here runs inside `operational_breaker` (core/breaker.py). BreakerOpen
is a RedisError, so a refusal takes the same branch: the two politeness guards
bypass, and the reservation still 503s.

ONE LUA SCRIPT FOR BOTH GUARDS (register items 27 and 119): the attempt cap and
the rate limit are checked and taken in one execution, and the check IS the
increment. Two judgements for one note cannot both take its one attempt, and
two for one subject cannot both take the last slot. It runs on fakeredis[lua]
in tests/unit/test_cost_lua.py.

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

from dodeal_ai.core import metrics
from dodeal_ai.core.breaker import breaker_field, operational_breaker
from dodeal_ai.core.redis import get_operational_client

_logger = logging.getLogger("dodeal_ai.unit_a.state")

# What an in-flight reservation holds: a fixed inert byte, told apart from a
# confirmed key's judgement JSON when a duplicate reads it back.
_RESERVED = "1"

# The TTL redis returns for a key that has no expiry set: the `-1` in the
# prompt-slots script. The M4 edge: a key that survived without a TTL would
# otherwise pin a user's rate limit or a note's attempt count forever.
_TTL_NO_EXPIRY = -1

# The script's first reply element: which guard refused the prompt, or neither.
# The Lua below returns these numbers; the two must be edited together.
_SLOTS_DENIED_BY_ATTEMPT = 0
_SLOTS_DENIED_BY_RATE = 1
_SLOTS_ALLOWED = 2


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


# Register item 66: the daily ceiling on the same subject, a separate key so it
# keeps its own window instead of sharing (and resetting) the hourly one.
def _rate_limit_day_key(tenant: str, subject: str) -> str:
    return f"ratelimit_day:{tenant}:{subject}"


# Register item 118: the note alone, never the lead. A lead id is the caller's
# to send, so a key that carried it gave every new lead id a fresh allowance.
def _attempt_key(tenant: str, note_id: int) -> str:
    return f"attempt:{tenant}:{note_id}"


def _attempt_fingerprint_key(tenant: str, note_id: int) -> str:
    """Beside the attempt counter, same (tenant, note), same TTL.

    A SECOND KEY rather than a hash holding both. The two are written in the
    same breath and expire on the same TTL, so the only thing a hash would add
    is that they expire on ONE ttl instead of two identical ones -- and it would
    turn the prompt-slots script's INCR on the counter into an HINCRBY. See the
    phase report for the full comparison.
    """
    return f"attempt_fp:{tenant}:{note_id}"


def _bypass(code: str, tenant: str, request_id: str, exc: BaseException) -> None:
    """One WARNING per bypassed call. Tenant and request_id only -- both are
    identifiers we already log at the gates, neither is note-derived -- plus
    `breaker: open` when the breaker refused rather than the store failing."""
    metrics.BYPASSES.labels(event=code).inc()
    _logger.warning(
        code,
        extra={
            "reason_code": code,
            "tenant": tenant,
            "request_id": request_id,
            **breaker_field(exc),
        },
    )


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
    judgement_json: str,
    ttl: int,
    request_id: str,
) -> None:
    """The judgement exists: store it under the key for the long TTL.

    `judgement_json` is the validated judgement and nothing else (items 1, 2).
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
            lambda: get_operational_client().set(key, judgement_json, xx=True, ex=ttl)
        )
    except redis.RedisError as exc:
        # No key material in the line, as for the reservation beside it.
        _bypass("idempotency_confirm_bypassed", tenant, request_id, exc)


async def read_confirmed_judgement(
    tenant: str,
    note_id: int,
    fingerprint: str,
    *,
    request_id: str,
) -> str | None:
    """The stored judgement JSON a duplicate replays, or None while the key is
    only reserved (or has gone). Untrusted until the caller validates it.

    Fails CLOSED like the reservation: an unanswered read raises
    IdempotencyUnavailableError rather than guess whether to judge.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        raw = await operational_breaker().call(
            lambda: get_operational_client().get(key)
        )
    except redis.RedisError as exc:
        _bypass("idempotency_unavailable", tenant, request_id, exc)
        raise IdempotencyUnavailableError() from None
    if raw is None or raw == _RESERVED:
        return None
    assert isinstance(raw, str)  # decode_responses=True, as for the reference
    return raw


# KEYS[1] the idempotency key. ARGV: the value read, the reservation marker, the
# short TTL. Replaces the value only if it is still the one read, so two
# duplicates that met the same invalid judgement cannot both take it over.
_TAKE_OVER_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""


async def take_over_idempotency(
    tenant: str,
    note_id: int,
    fingerprint: str,
    *,
    expected: str,
    ttl: int,
    request_id: str,
) -> bool:
    """Turn a stored value that no longer validates back into a reservation.

    True if WE took it: the caller judges again. False if the value changed
    since it was read: someone else has it, and the caller answers 409.
    Fails CLOSED, as the reservation does.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        taken = await operational_breaker().call(
            lambda: get_operational_client().eval(
                _TAKE_OVER_SCRIPT, 1, key, expected, _RESERVED, ttl
            )
        )
    except redis.RedisError as exc:
        _bypass("idempotency_unavailable", tenant, request_id, exc)
        raise IdempotencyUnavailableError() from None
    return int(taken) == 1


# --- the attempt cap and the rate limit, taken together: fails OPEN ---------


# KEYS[1] the note's attempt counter, KEYS[2] the subject's hourly rate limit,
# KEYS[3] the subject's daily rate limit (register item 66). ARGV: attempt cap,
# attempt TTL, hourly limit, hourly window, daily limit, daily window. A refusal
# writes nothing; a take INCRs all three and sets a window on a key that is new
# or has none (audit M4). The daily check runs after the hourly one and denies
# with the SAME outcome code -- one withheld reason, `rate_limited`, for either.
_TAKE_PROMPT_SLOTS_SCRIPT = """
local attempts = tonumber(redis.call('GET', KEYS[1]) or '0')
if attempts >= tonumber(ARGV[1]) then
  return {0, attempts, 0}
end
local rate = tonumber(redis.call('GET', KEYS[2]) or '0')
if rate >= tonumber(ARGV[3]) then
  return {1, attempts, rate}
end
local rate_day = tonumber(redis.call('GET', KEYS[3]) or '0')
if rate_day >= tonumber(ARGV[5]) then
  -- The third value is always the HOURLY count, on every path: decide()
  -- compares it against rate_limit_per_hour. The daily guard denies with the
  -- same `rate_limited` reason and does not change what this slot means.
  return {1, attempts, rate}
end
local taken = redis.call('INCR', KEYS[1])
if taken == 1 or redis.call('TTL', KEYS[1]) == -1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
local rate_taken = redis.call('INCR', KEYS[2])
if rate_taken == 1 or redis.call('TTL', KEYS[2]) == -1 then
  redis.call('EXPIRE', KEYS[2], ARGV[4])
end
local rate_day_taken = redis.call('INCR', KEYS[3])
if rate_day_taken == 1 or redis.call('TTL', KEYS[3]) == -1 then
  redis.call('EXPIRE', KEYS[3], ARGV[6])
end
return {2, taken, rate}
"""


def _prompt_slots_answer(reply: list[int]) -> tuple[int, bool, int]:
    """The script's reply as decide() reads it: (attempts, rate_allowed, rate_count).

    Both counts are BEFORE this request. The script hands back the attempt count
    AFTER its INCR on a take, so one comes off it there; a refusal changed
    nothing, and the stored count is already the one before.
    """
    outcome, attempts, rate_count = (int(value) for value in reply)
    if outcome == _SLOTS_ALLOWED:
        return attempts - 1, True, rate_count
    return attempts, outcome != _SLOTS_DENIED_BY_RATE, rate_count


async def take_prompt_slots(
    tenant: str,
    note_id: int,
    subject: str,
    *,
    attempt_cap: int,
    attempt_ttl: int,
    rate_limit: int,
    rate_ttl: int,
    rate_limit_day: int,
    rate_ttl_day: int,
    attempts_read: int,
    request_id: str,
) -> tuple[int, bool, int]:
    """Claim this note's next attempt and this subject's next prompt, or neither.

    The only writer of the three counters, called only where a prompt would be
    sent, in one round trip (register items 66 and 119). Returns (attempts,
    rate_allowed, rate_count), both counts BEFORE this call. A request that lost
    the race for the note's attempt gets the stored count back, so decide() says
    attempt_cap.

    `rate_limit`/`rate_ttl` are the hourly guard, `rate_limit_day`/`rate_ttl_day`
    the daily one beside it (register item 66); either denies with the same
    `rate_limited` reason, so the caller need not tell them apart.

    Fails OPEN at (attempts_read, True, 0) -- the attempts read before the model
    ran, and an open window -- with both guards' own bypass codes.
    """
    try:
        reply = await operational_breaker().call(
            lambda: get_operational_client().eval(
                _TAKE_PROMPT_SLOTS_SCRIPT,
                3,
                _attempt_key(tenant, note_id),
                _rate_limit_key(tenant, subject),
                _rate_limit_day_key(tenant, subject),
                attempt_cap,
                attempt_ttl,
                rate_limit,
                rate_ttl,
                rate_limit_day,
                rate_ttl_day,
            )
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)
        _bypass("rate_limit_bypassed", tenant, request_id, exc)
        return attempts_read, True, 0
    return _prompt_slots_answer(reply)


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


async def read_attempts(tenant: str, note_id: int, *, request_id: str) -> int:
    """How many clarification prompts this note has already drawn.

    PROVISIONAL: it picks which trip take_prompt_slots makes, and that script
    checks the cap again as it takes, so a concurrent request cannot slip past.
    0 when the store is unreachable (fail open) or the key has expired.
    """
    try:
        raw = await operational_breaker().call(
            lambda: get_operational_client().get(_attempt_key(tenant, note_id))
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)
        return 0
    return int(raw or 0)


# --- the resubmission reference (register item 33): also fails OPEN --------


async def write_attempt_fingerprint(
    tenant: str,
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
                _attempt_fingerprint_key(tenant, note_id),
                fingerprint,
                nx=True,
                ex=ttl,
            )
        )
    except redis.RedisError as exc:
        _bypass("attempt_counter_bypassed", tenant, request_id, exc)


async def read_attempt_fingerprint(
    tenant: str, note_id: int, *, request_id: str
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
                _attempt_fingerprint_key(tenant, note_id)
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
