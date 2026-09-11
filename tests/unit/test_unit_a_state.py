"""Unit A operational state on db2: idempotency reserves once and fails CLOSED,
the two counters fail OPEN with a log line, windows are set on create and
repaired when a key has none, and no key value ever reaches a log line.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.state import (
    IdempotencyUnavailableError,
    increment_attempts,
    note_fingerprint,
    read_attempt_fingerprint,
    read_attempts,
    read_rate_limit,
    release_idempotency,
    reserve_idempotency,
    take_rate_limit,
    write_attempt_fingerprint,
)
from tests.helpers.fake_operational_redis import (
    TTL_NO_EXPIRY,
    FakeOperationalRedis,
)

TENANT = "tenant-a"
SUBJECT = "42"
LEAD_ID = 1656
NOTE_ID = 10
REQUEST_ID = "req-1"

IDEM_TTL = 86400
WINDOW = 3600
# The rate limit is now checked BY taking a slot, so every call needs the cap
# it is checked against. 3 is TenantConfig's default.
LIMIT = 3
ATTEMPT_TTL = 21600

# Shaped like a real fingerprint: 64 hex chars. Bound to a NAME here and only
# ever passed by name, never written as a literal on a line that could end up
# quoted in a traceback frame -- the discipline from
# tests/security/test_log_safety.py::_raise_foreign.
SENTINEL_FINGERPRINT = "5e" * 32


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeOperationalRedis:
    client = FakeOperationalRedis()
    monkeypatch.setattr(state, "get_operational_client", lambda: client)
    return client


@pytest.fixture
def failing(monkeypatch: pytest.MonkeyPatch):
    """A client whose named commands raise RedisError."""

    def _build(*commands: str) -> FakeOperationalRedis:
        client = FakeOperationalRedis(raise_on=set(commands))
        monkeypatch.setattr(state, "get_operational_client", lambda: client)
        return client

    return _build


@pytest.fixture
def json_log():
    """The real JsonFormatter over the whole dodeal_ai tree -- the exact text a
    log collector would receive. Copied from tests/security/conftest.py."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


# --- the fingerprint -------------------------------------------------------


def test_fingerprint_is_hex_sha256_of_utf8() -> None:
    import hashlib

    text = "Client wants 3BR in New Cairo."
    assert note_fingerprint(text) == hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert len(note_fingerprint(text)) == 64


def test_fingerprint_does_not_normalise() -> None:
    # An edited note is a different note and must be judged again, not
    # collide with the previous reservation.
    assert note_fingerprint("Called.") != note_fingerprint("called.")
    assert note_fingerprint("Called.") != note_fingerprint(" Called. ")


def test_fingerprint_handles_non_ascii() -> None:
    # One prompt set serves AR/EN/mixed; the fingerprint must not care.
    arabic = "اتصلت بالعميل ولم يرد"
    assert len(note_fingerprint(arabic)) == 64
    assert note_fingerprint(arabic) != note_fingerprint(arabic + " ")


# --- idempotency: reserve once ---------------------------------------------


async def test_first_reservation_is_claimed(fake: FakeOperationalRedis) -> None:
    claimed = await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    assert claimed is True


async def test_second_identical_reservation_is_refused(
    fake: FakeOperationalRedis,
) -> None:
    await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    again = await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    assert again is False


async def test_reservation_sets_the_ttl_on_create(fake: FakeOperationalRedis) -> None:
    await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    key = next(iter(fake.store))
    assert fake.ttls[key] == IDEM_TTL


async def test_a_new_fingerprint_is_a_new_judgement_not_a_duplicate(
    fake: FakeOperationalRedis,
) -> None:
    # The note was edited: same note_id, different text.
    await reserve_idempotency(
        TENANT,
        NOTE_ID,
        note_fingerprint("first text"),
        ttl=IDEM_TTL,
        request_id=REQUEST_ID,
    )
    claimed = await reserve_idempotency(
        TENANT,
        NOTE_ID,
        note_fingerprint("edited text"),
        ttl=IDEM_TTL,
        request_id=REQUEST_ID,
    )
    assert claimed is True


async def test_reservations_are_scoped_per_tenant(fake: FakeOperationalRedis) -> None:
    await reserve_idempotency(
        "tenant-a", NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    claimed = await reserve_idempotency(
        "tenant-b", NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    # Same note id and same text in another tenant is a different judgement.
    assert claimed is True


async def test_reservation_stores_no_note_derived_value(
    fake: FakeOperationalRedis,
) -> None:
    await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    assert set(fake.store.values()) == {"1"}


# --- idempotency: fails CLOSED ---------------------------------------------


async def test_reserve_raises_when_the_store_is_unreachable(failing) -> None:
    failing("set")
    with pytest.raises(IdempotencyUnavailableError) as exc:
        await reserve_idempotency(
            TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
        )
    assert exc.value.reason_code == "idempotency_unavailable"
    assert str(exc.value) == "idempotency_unavailable"


async def test_reserve_does_not_chain_the_redis_error(failing) -> None:
    # from None: a chained RedisError would put its message -- which can quote
    # the command, and so the key -- into any traceback formatted downstream.
    failing("set")
    with pytest.raises(IdempotencyUnavailableError) as exc:
        await reserve_idempotency(
            TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
        )
    assert exc.value.__cause__ is None


# --- release: best effort --------------------------------------------------


async def test_release_removes_the_reservation(fake: FakeOperationalRedis) -> None:
    await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    await release_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, request_id=REQUEST_ID
    )
    assert fake.store == {}

    # ... and the same request can now be retried.
    claimed = await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )
    assert claimed is True


async def test_release_swallows_a_redis_error(failing) -> None:
    failing("delete")
    # Must not raise: it runs on an error path and would otherwise replace the
    # real failure with a less useful one.
    await release_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, request_id=REQUEST_ID
    )


# --- rate limit ------------------------------------------------------------


async def test_rate_limit_starts_at_zero(fake: FakeOperationalRedis) -> None:
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 0


async def _take(fake_limit: int = LIMIT) -> tuple[bool, int]:
    """One slot, at the default cap unless a case needs another."""
    return await take_rate_limit(
        TENANT, SUBJECT, limit=fake_limit, ttl=WINDOW, request_id=REQUEST_ID
    )


async def test_rate_limit_counts_prompts_sent(fake: FakeOperationalRedis) -> None:
    for _ in range(3):
        await _take()
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 3


async def test_taking_a_slot_returns_the_count_before_it(
    fake: FakeOperationalRedis,
) -> None:
    """Each slot returns the count before this request, which is what decide() compares
    to the cap."""
    assert await _take() == (True, 0)
    assert await _take() == (True, 1)
    assert await _take() == (True, 2)


async def test_the_slot_at_the_cap_is_refused_and_not_counted(
    fake: FakeOperationalRedis,
) -> None:
    """A slot refused at the cap leaves the counter where it was."""
    for _ in range(LIMIT):
        await _take()

    assert await _take() == (False, LIMIT)
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == LIMIT


async def test_rate_limit_window_is_set_on_create(fake: FakeOperationalRedis) -> None:
    await _take()
    assert fake.ttls[f"ratelimit:{TENANT}:{SUBJECT}"] == WINDOW


async def test_rate_limit_is_keyed_on_the_subject_and_the_tenant(
    fake: FakeOperationalRedis,
) -> None:
    await _take()
    assert f"ratelimit:{TENANT}:{SUBJECT}" in fake.store
    # A different subject in the same tenant is counted separately.
    assert await read_rate_limit(TENANT, "99", request_id=REQUEST_ID) == 0
    # ... and so is the same subject in another tenant.
    assert await read_rate_limit("tenant-b", SUBJECT, request_id=REQUEST_ID) == 0


async def test_rate_limit_does_not_reset_the_window_on_later_increments(
    fake: FakeOperationalRedis,
) -> None:
    await _take()
    key = f"ratelimit:{TENANT}:{SUBJECT}"
    fake.ttls[key] = 60  # the window has been running a while
    await _take()
    # A sliding window would let a busy user never hit the cap.
    assert fake.ttls[key] == 60


async def test_one_slot_is_one_round_trip(fake: FakeOperationalRedis) -> None:
    """Register item 27: the read-then-increment pair is one EVAL now."""
    await _take()
    assert [c for c, _ in fake.commands] == ["eval"]


# --- the M4 edge, carried to db2 -------------------------------------------


async def test_a_key_with_no_ttl_gets_one_on_the_next_slot(
    fake: FakeOperationalRedis,
) -> None:
    # Audit M4 on db2: a counter that somehow exists WITHOUT an expiry would
    # otherwise pin this user's rate limit forever, silently withholding every
    # future clarification prompt. The script carries the guard now.
    key = f"ratelimit:{TENANT}:{SUBJECT}"
    fake.store[key] = "2"  # exists, no entry in ttls -> TTL == -1
    assert await fake.ttl(key) == TTL_NO_EXPIRY

    await _take()

    assert fake.ttls[key] == WINDOW


async def test_an_attempt_key_with_no_ttl_gets_one_too(
    fake: FakeOperationalRedis,
) -> None:
    key = f"attempt:{TENANT}:{LEAD_ID}:{NOTE_ID}"
    fake.store[key] = "1"
    await increment_attempts(TENANT, LEAD_ID, NOTE_ID, ttl=21600, request_id=REQUEST_ID)
    assert fake.ttls[key] == 21600


async def test_a_new_key_does_not_pay_for_a_ttl_lookup(
    fake: FakeOperationalRedis,
) -> None:
    # The `or` short-circuits: count == 1 means the key is new, so EXPIRE is
    # issued without asking for the TTL first.
    await increment_attempts(TENANT, LEAD_ID, NOTE_ID, ttl=21600, request_id=REQUEST_ID)
    assert [c for c, _ in fake.commands] == ["incr", "expire"]


# --- attempts --------------------------------------------------------------


async def test_attempts_start_at_zero(fake: FakeOperationalRedis) -> None:
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 0


async def test_attempts_count_per_note(fake: FakeOperationalRedis) -> None:
    await increment_attempts(TENANT, LEAD_ID, NOTE_ID, ttl=21600, request_id=REQUEST_ID)
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 1
    # A different note on the same lead is counted separately.
    assert await read_attempts(TENANT, LEAD_ID, 11, request_id=REQUEST_ID) == 0


async def test_attempt_key_carries_tenant_lead_and_note(
    fake: FakeOperationalRedis,
) -> None:
    await increment_attempts(TENANT, LEAD_ID, NOTE_ID, ttl=21600, request_id=REQUEST_ID)
    assert f"attempt:{TENANT}:{LEAD_ID}:{NOTE_ID}" in fake.store


# --- the resubmission reference (register item 33) --------------------------


async def test_the_reference_starts_absent(fake: FakeOperationalRedis) -> None:
    assert (
        await read_attempt_fingerprint(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID)
        is None
    )


async def test_the_reference_reads_back_what_was_written(
    fake: FakeOperationalRedis,
) -> None:
    await write_attempt_fingerprint(
        TENANT,
        LEAD_ID,
        NOTE_ID,
        SENTINEL_FINGERPRINT,
        ttl=ATTEMPT_TTL,
        request_id=REQUEST_ID,
    )
    assert (
        await read_attempt_fingerprint(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID)
        == SENTINEL_FINGERPRINT
    )


async def test_the_reference_lives_beside_the_counter_on_the_same_ttl(
    fake: FakeOperationalRedis,
) -> None:
    # The two are written in the same breath and must die together: a reference
    # that outlived its counter would point at a judgement whose attempt state
    # is gone.
    await increment_attempts(
        TENANT, LEAD_ID, NOTE_ID, ttl=ATTEMPT_TTL, request_id=REQUEST_ID
    )
    await write_attempt_fingerprint(
        TENANT,
        LEAD_ID,
        NOTE_ID,
        SENTINEL_FINGERPRINT,
        ttl=ATTEMPT_TTL,
        request_id=REQUEST_ID,
    )

    counter = f"attempt:{TENANT}:{LEAD_ID}:{NOTE_ID}"
    reference = f"attempt_fp:{TENANT}:{LEAD_ID}:{NOTE_ID}"
    assert reference in fake.store
    assert fake.ttls[reference] == fake.ttls[counter] == ATTEMPT_TTL


async def test_the_reference_is_the_first_one_written(
    fake: FakeOperationalRedis,
) -> None:
    # SET NX. The field is specified as the note as it was FIRST prompted on;
    # the cap makes a second prompt impossible today, and NX makes the claim
    # true by construction rather than by the cap's current value.
    other = "a1" * 32
    await write_attempt_fingerprint(
        TENANT,
        LEAD_ID,
        NOTE_ID,
        SENTINEL_FINGERPRINT,
        ttl=ATTEMPT_TTL,
        request_id=REQUEST_ID,
    )
    await write_attempt_fingerprint(
        TENANT, LEAD_ID, NOTE_ID, other, ttl=ATTEMPT_TTL, request_id=REQUEST_ID
    )
    assert (
        await read_attempt_fingerprint(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID)
        == SENTINEL_FINGERPRINT
    )


async def test_the_reference_is_scoped_per_tenant_lead_and_note(
    fake: FakeOperationalRedis,
) -> None:
    await write_attempt_fingerprint(
        TENANT,
        LEAD_ID,
        NOTE_ID,
        SENTINEL_FINGERPRINT,
        ttl=ATTEMPT_TTL,
        request_id=REQUEST_ID,
    )
    assert f"attempt_fp:{TENANT}:{LEAD_ID}:{NOTE_ID}" in fake.store
    for tenant, lead_id, note_id in (
        ("tenant-b", LEAD_ID, NOTE_ID),
        (TENANT, 1657, NOTE_ID),
        (TENANT, LEAD_ID, 11),
    ):
        assert (
            await read_attempt_fingerprint(
                tenant, lead_id, note_id, request_id=REQUEST_ID
            )
            is None
        )


async def test_the_reference_fails_open_on_both_sides(failing, json_log) -> None:
    # No new bypass code: this is the attempt counter's state, and it borrows
    # the attempt counter's policy and its one code.
    failing("get", "set")

    await write_attempt_fingerprint(
        TENANT,
        LEAD_ID,
        NOTE_ID,
        SENTINEL_FINGERPRINT,
        ttl=ATTEMPT_TTL,
        request_id=REQUEST_ID,
    )
    assert (
        await read_attempt_fingerprint(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID)
        is None
    )

    codes = [line["reason_code"] for line in _lines(json_log)]
    assert codes == ["attempt_counter_bypassed", "attempt_counter_bypassed"]


async def test_no_reference_fingerprint_reaches_a_log_line(failing, json_log) -> None:
    # The value stored here IS a note fingerprint -- the one place in this
    # module where the digest is a VALUE rather than part of a key -- so the
    # sentinel matters more here than anywhere else.
    failing("get", "set")

    await write_attempt_fingerprint(
        TENANT,
        LEAD_ID,
        NOTE_ID,
        SENTINEL_FINGERPRINT,
        ttl=ATTEMPT_TTL,
        request_id=REQUEST_ID,
    )
    await read_attempt_fingerprint(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID)

    text = json_log.getvalue()
    assert SENTINEL_FINGERPRINT not in text
    assert "attempt_fp:" not in text


# --- the two OPEN failure policies -----------------------------------------


async def test_read_rate_limit_fails_open_with_a_log_line(failing, json_log) -> None:
    failing("get")
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 0

    line = _lines(json_log)[-1]
    assert line["level"] == "WARNING"
    assert line["reason_code"] == "rate_limit_bypassed"
    assert line["tenant"] == TENANT
    assert line["request_id"] == REQUEST_ID


async def test_take_rate_limit_fails_open_with_a_log_line(failing, json_log) -> None:
    """An unreachable store costs a question too many, never a refusal: the
    slot is granted and simply not counted."""
    failing("eval")
    assert await _take() == (True, 0)

    assert _lines(json_log)[-1]["reason_code"] == "rate_limit_bypassed"


async def test_read_attempts_fails_open_with_a_log_line(failing, json_log) -> None:
    failing("get")
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 0

    assert _lines(json_log)[-1]["reason_code"] == "attempt_counter_bypassed"


async def test_increment_attempts_fails_open_with_a_log_line(failing, json_log) -> None:
    failing("incr")
    await increment_attempts(TENANT, LEAD_ID, NOTE_ID, ttl=21600, request_id=REQUEST_ID)

    assert _lines(json_log)[-1]["reason_code"] == "attempt_counter_bypassed"


async def test_a_bypass_logs_exactly_one_line_per_call(failing, json_log) -> None:
    failing("get")
    await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID)
    await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID)

    bypasses = [
        line
        for line in _lines(json_log)
        if line["reason_code"] == "rate_limit_bypassed"
    ]
    assert len(bypasses) == 2  # once per call, never deduplicated


async def test_the_three_policies_are_not_the_same(failing) -> None:
    # The whole point of this module: one store, three policies.
    failing("set", "get", "incr")
    with pytest.raises(IdempotencyUnavailableError):
        await reserve_idempotency(
            TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
        )
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 0
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 0


# --- sentinel: no key value reaches a log line -----------------------------


async def test_no_fingerprint_reaches_a_log_line(failing, json_log) -> None:
    # The idempotency key embeds a fingerprint of the note text. A fingerprint
    # in the log stream is a stable identifier for one specific note body, and
    # the store failure path is the one place a key could be interpolated into
    # a message by mistake.
    failing("set")
    with pytest.raises(IdempotencyUnavailableError):
        await reserve_idempotency(
            TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
        )

    text = json_log.getvalue()
    assert SENTINEL_FINGERPRINT not in text
    assert "idem:" not in text  # nor the key prefix it would arrive with

    line = _lines(json_log)[-1]
    assert line["reason_code"] == "idempotency_unavailable"
    assert line["tenant"] == TENANT
    assert line["request_id"] == REQUEST_ID


async def test_the_error_message_is_a_fixed_reason_code_only(failing) -> None:
    # log_safety keeps the message of any exception defined under dodeal_ai,
    # so this one must never interpolate the key, the fingerprint or the note.
    failing("set")
    try:
        await reserve_idempotency(
            TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
        )
    except IdempotencyUnavailableError as exc:
        message = str(exc)
    else:  # pragma: no cover - the store is down by construction
        pytest.fail("expected IdempotencyUnavailableError")

    assert message == "idempotency_unavailable"
    assert SENTINEL_FINGERPRINT not in message


async def test_a_release_failure_logs_no_key_either(failing, json_log) -> None:
    failing("delete")
    await release_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, request_id=REQUEST_ID
    )

    text = json_log.getvalue()
    assert SENTINEL_FINGERPRINT not in text
    assert _lines(json_log)[-1]["reason_code"] == "idempotency_release_failed"
