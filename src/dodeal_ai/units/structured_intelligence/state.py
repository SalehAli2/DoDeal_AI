"""Unit A's three operational state concerns, all on the db2 connection.

  idempotency  Has this exact judgement already been done? Reserved once, per
               (tenant, note, note-text fingerprint).
  rate limit   How many clarification prompts has this USER been sent in the
               window? Keyed on the subject asking, never on the note's author.
  attempts     How many clarification prompts has THIS note already drawn?

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

SEQUENTIAL COMMANDS, NO LUA. Audit finding M5 records that no Lua script in
this repo is executed by the test suite (fakeredis[lua] arrives at step 3), so
a script here would be untested logic guarding paid work. The counter pattern
below (INCR, then EXPIRE only when the key is new or has no TTL) is not atomic
across the two commands, and does not need to be: a lost race re-sets the same
TTL on the same key. Contrast core/cost/limiter.py, where the tenant and user
counters must move together and Lua is therefore load-bearing.

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
from redis import asyncio as aioredis

from dodeal_ai.core.redis import get_operational_client

_logger = logging.getLogger("dodeal_ai.unit_a.state")

# A reservation only has to EXIST; nothing ever reads its value back. A fixed
# inert byte keeps note-derived content out of the store as well as the log.
_RESERVED = "1"

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


def _bypass(code: str, tenant: str, request_id: str) -> None:
    """One WARNING per bypassed call. Tenant and request_id only -- both are
    identifiers we already log at the gates, neither is note-derived."""
    _logger.warning(
        code,
        extra={"reason_code": code, "tenant": tenant, "request_id": request_id},
    )


async def _incr_with_window(client: aioredis.Redis, key: str, ttl: int) -> int:
    """INCR, then set the window's expiry when the key is NEW or has no TTL.

    The `or` short-circuits, so a brand-new key costs one INCR and one EXPIRE
    and never calls TTL. The TTL == -1 branch is audit finding M4 carried over
    to db2: the cost limiter's Lua sets EXPIRE only on create, so a key that
    somehow exists without a TTL never expires. Here that key would pin a
    user's rate limit -- or a note's attempt count -- forever, silently
    withholding every future clarification prompt.
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

    Raises IdempotencyUnavailableError on any RedisError -- see the module
    docstring for why this one does not fail open.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        claimed = await get_operational_client().set(key, _RESERVED, nx=True, ex=ttl)
    except redis.RedisError:
        # No key material in the line: the key embeds the fingerprint.
        _bypass("idempotency_unavailable", tenant, request_id)
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

    Called on every non-200 after a successful reservation, so that a caller
    whose judgement failed for a reason of ours (the model was down, the
    backend was down, the output would not validate) can simply retry instead
    of being told 409 for the next 24 hours. If the release itself fails the
    reservation just expires on its TTL -- annoying, never wrong -- and raising
    here would replace the real error with a less useful one.
    """
    key = _idempotency_key(tenant, note_id, fingerprint)
    try:
        await get_operational_client().delete(key)
    except redis.RedisError:
        _bypass("idempotency_release_failed", tenant, request_id)


# --- rate limit: fails OPEN ------------------------------------------------


async def read_rate_limit(tenant: str, subject: str, *, request_id: str) -> int:
    """How many clarification prompts this subject has been sent this window.

    ASSUMPTION[Q7]: keyed on the JWT subject -- who is ASKING -- not on the
    note's author_id. The limit exists to stop us pestering one person, and the
    person we would pester is the one making the request.

    0 when the store is unreachable (fail open) or the key has expired.
    """
    try:
        raw = await get_operational_client().get(_rate_limit_key(tenant, subject))
    except redis.RedisError:
        _bypass("rate_limit_bypassed", tenant, request_id)
        return 0
    return int(raw or 0)


async def increment_rate_limit(
    tenant: str, subject: str, *, ttl: int, request_id: str
) -> None:
    """Count one clarification prompt against this subject's window.

    Called ONLY when a prompt is actually sent -- never on a judgement that
    withheld one, and never on a resubmission. A counter that moved on a
    withheld prompt would rate-limit a user for messages they never received.
    """
    try:
        await _incr_with_window(
            get_operational_client(), _rate_limit_key(tenant, subject), ttl
        )
    except redis.RedisError:
        _bypass("rate_limit_bypassed", tenant, request_id)


# --- attempts: fails OPEN --------------------------------------------------


async def read_attempts(
    tenant: str, lead_id: int, note_id: int, *, request_id: str
) -> int:
    """How many clarification prompts this note has already drawn.

    0 when the store is unreachable (fail open) or the key has expired.
    """
    try:
        raw = await get_operational_client().get(_attempt_key(tenant, lead_id, note_id))
    except redis.RedisError:
        _bypass("attempt_counter_bypassed", tenant, request_id)
        return 0
    return int(raw or 0)


async def increment_attempts(
    tenant: str, lead_id: int, note_id: int, *, ttl: int, request_id: str
) -> None:
    """Count one clarification prompt against this note. Same rule as the rate
    limit: only when a prompt is actually sent."""
    try:
        await _incr_with_window(
            get_operational_client(), _attempt_key(tenant, lead_id, note_id), ttl
        )
    except redis.RedisError:
        _bypass("attempt_counter_bypassed", tenant, request_id)
