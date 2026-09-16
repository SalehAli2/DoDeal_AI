"""Unit A operational state on db2: idempotency reserves once and fails CLOSED,
the two counters fail OPEN with a log line, windows are set on create and
repaired when a key has none, and no key value ever reaches a log line.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from dodeal_ai.core.breaker import operational_breaker
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.state import (
    IdempotencyUnavailableError,
    confirm_idempotency,
    note_fingerprint,
    read_attempt_fingerprint,
    read_attempts,
    read_rate_limit,
    release_idempotency,
    reserve_idempotency,
    take_prompt_slots,
    write_attempt_fingerprint,
)
from tests.helpers import breakers
from tests.helpers.fake_operational_redis import FakeOperationalRedis

TENANT = "tenant-a"
SUBJECT = "42"
LEAD_ID = 1656
NOTE_ID = 10
REQUEST_ID = "req-1"

IDEM_TTL = 86400
WINDOW = 3600
# Both guards are checked BY taking a slot, so every take needs the caps it is
# checked against. 3 and 1 are TenantConfig's defaults.
LIMIT = 3
CAP = 1
ATTEMPT_TTL = 21600
ATTEMPT_KEY = f"attempt:{TENANT}:{LEAD_ID}:{NOTE_ID}"
RATE_KEY = f"ratelimit:{TENANT}:{SUBJECT}"

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


# --- confirm: the long lifetime, once the judgement exists (item 82) -------

# The pipeline's in-flight TTL at the default deadline; IDEM_TTL above is the
# long one the confirm applies.
INFLIGHT_TTL = 100

# What the JSON formatter puts on every line; anything else came from extra=.
_FORMATTER_FIELDS = {"message", "timestamp", "level", "logger"}


async def _confirm() -> None:
    await confirm_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
    )


async def test_confirm_replaces_the_reservation_with_the_long_ttl(
    fake: FakeOperationalRedis,
) -> None:
    await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=INFLIGHT_TTL, request_id=REQUEST_ID
    )
    [key] = fake.store
    assert (fake.store[key], fake.ttls[key]) == ("1", INFLIGHT_TTL)

    await _confirm()

    # "done", not "1": the two states are told apart in a console.
    assert (fake.store[key], fake.ttls[key]) == ("done", IDEM_TTL)


async def test_confirm_uses_xx_and_never_creates_a_key(
    fake: FakeOperationalRedis,
) -> None:
    """A reservation that has already expired stays gone. Writing it back would
    lock the note on a reservation nobody holds."""
    await _confirm()

    assert fake.store == {}
    assert [c for c, _ in fake.commands] == ["set"]  # asked, and declined


async def test_a_confirmed_key_is_still_a_duplicate(fake: FakeOperationalRedis) -> None:
    await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=INFLIGHT_TTL, request_id=REQUEST_ID
    )
    await _confirm()

    again = await reserve_idempotency(
        TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=INFLIGHT_TTL, request_id=REQUEST_ID
    )
    assert again is False


async def test_confirm_fails_open_with_a_line_of_ids_only(failing, json_log) -> None:
    """The one idempotency call that does not fail closed: the judgement is
    already built. Its line names the tenant and the request and nothing else --
    no key, and no fingerprint inside one."""
    failing("set")

    await _confirm()  # does not raise

    text = json_log.getvalue()
    assert SENTINEL_FINGERPRINT not in text
    assert "idem:" not in text
    line = _lines(json_log)[-1]
    assert line["level"] == "WARNING"
    assert line["message"] == "idempotency_confirm_bypassed"
    assert set(line) - _FORMATTER_FIELDS == {"reason_code", "tenant", "request_id"}
    assert (line["reason_code"], line["tenant"], line["request_id"]) == (
        "idempotency_confirm_bypassed",
        TENANT,
        REQUEST_ID,
    )


async def test_an_open_breaker_bypasses_the_confirm_without_asking(
    fake: FakeOperationalRedis, json_log
) -> None:
    """Under the operational breaker like every other call here: refused without
    a SET, and the same bypass line says so."""
    await breakers.trip(operational_breaker())

    await _confirm()

    assert fake.commands == []
    line = _lines(json_log)[-1]
    assert (line["reason_code"], line["breaker"]) == (
        "idempotency_confirm_bypassed",
        "open",
    )


# --- the attempt cap and the rate limit, taken together (item 119) ----------


async def test_rate_limit_starts_at_zero(fake: FakeOperationalRedis) -> None:
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 0


async def _take(
    note_id: int = NOTE_ID,
    *,
    cap: int = CAP,
    attempts_read: int = 0,
) -> tuple[int, bool, int]:
    """One take for this subject, at the default caps unless a case needs others."""
    return await take_prompt_slots(
        TENANT,
        LEAD_ID,
        note_id,
        SUBJECT,
        attempt_cap=cap,
        attempt_ttl=ATTEMPT_TTL,
        rate_limit=LIMIT,
        rate_ttl=WINDOW,
        attempts_read=attempts_read,
        request_id=REQUEST_ID,
    )


async def test_a_take_counts_one_attempt_and_one_rate_slot(
    fake: FakeOperationalRedis,
) -> None:
    """An allowed take moves both counters by one."""
    assert await _take() == (0, True, 0)
    assert (fake.store[ATTEMPT_KEY], fake.store[RATE_KEY]) == ("1", "1")


async def test_rate_limit_counts_prompts_sent(fake: FakeOperationalRedis) -> None:
    for note_id in (10, 11, 12):
        await _take(note_id)
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 3


async def test_taking_slots_returns_the_counts_before_it(
    fake: FakeOperationalRedis,
) -> None:
    """Both counts come back as they were before this take, which is what decide()
    compares to the caps."""
    assert await _take(10) == (0, True, 0)
    assert await _take(11) == (0, True, 1)
    assert await _take(12) == (0, True, 2)


async def test_the_attempt_cap_refuses_and_leaves_the_rate_key_untouched(
    fake: FakeOperationalRedis,
) -> None:
    """At the attempt cap the take is refused, the stored count comes back, and no
    rate slot is taken or even created."""
    fake.store[ATTEMPT_KEY] = str(CAP)

    assert await _take() == (CAP, True, 0)
    assert fake.store == {ATTEMPT_KEY: str(CAP)}
    assert fake.ttls == {}


async def test_the_rate_limit_refuses_and_leaves_the_attempt_key_untouched(
    fake: FakeOperationalRedis,
) -> None:
    """At the rate limit the take is refused and no attempt is counted or created."""
    fake.store[RATE_KEY] = str(LIMIT)

    assert await _take() == (0, False, LIMIT)
    assert fake.store == {RATE_KEY: str(LIMIT)}
    assert fake.ttls == {}


async def test_a_second_take_on_one_note_gets_the_cap_back(
    fake: FakeOperationalRedis,
) -> None:
    """The race, played in order: the second take reads the first one's attempt."""
    assert await _take() == (0, True, 0)
    assert await _take() == (CAP, True, 0)
    assert (fake.store[ATTEMPT_KEY], fake.store[RATE_KEY]) == ("1", "1")


async def test_both_windows_are_set_on_create(fake: FakeOperationalRedis) -> None:
    await _take()
    assert (fake.ttls[ATTEMPT_KEY], fake.ttls[RATE_KEY]) == (ATTEMPT_TTL, WINDOW)


async def test_rate_limit_is_keyed_on_the_subject_and_the_tenant(
    fake: FakeOperationalRedis,
) -> None:
    await _take()
    assert RATE_KEY in fake.store
    # A different subject in the same tenant is counted separately.
    assert await read_rate_limit(TENANT, "99", request_id=REQUEST_ID) == 0
    # ... and so is the same subject in another tenant.
    assert await read_rate_limit("tenant-b", SUBJECT, request_id=REQUEST_ID) == 0


async def test_a_later_take_does_not_reset_either_window(
    fake: FakeOperationalRedis,
) -> None:
    await _take(cap=2)
    fake.ttls[ATTEMPT_KEY] = 60  # both windows have been running a while
    fake.ttls[RATE_KEY] = 60
    await _take(cap=2)
    # A sliding window would let a busy user never hit the cap.
    assert (fake.ttls[ATTEMPT_KEY], fake.ttls[RATE_KEY]) == (60, 60)


async def test_one_take_is_one_round_trip(fake: FakeOperationalRedis) -> None:
    """Register item 119: both checks and both increments are one EVAL."""
    await _take()
    assert [c for c, _ in fake.commands] == ["eval"]


# --- the M4 edge, carried to db2 -------------------------------------------


async def test_keys_with_no_ttl_get_one_on_the_next_take(
    fake: FakeOperationalRedis,
) -> None:
    """Audit M4 on db2: a counter with no expiry gains its window on the next take,
    rather than pinning a user's rate limit or a note's attempts forever."""
    fake.store[ATTEMPT_KEY] = "1"  # exists, no entry in ttls -> TTL == -1
    fake.store[RATE_KEY] = "2"

    await _take(cap=2)

    assert (fake.ttls[ATTEMPT_KEY], fake.ttls[RATE_KEY]) == (ATTEMPT_TTL, WINDOW)


# --- attempts --------------------------------------------------------------


async def test_attempts_start_at_zero(fake: FakeOperationalRedis) -> None:
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 0


async def test_attempts_count_per_note(fake: FakeOperationalRedis) -> None:
    await _take()
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 1
    # A different note on the same lead is counted separately.
    assert await read_attempts(TENANT, LEAD_ID, 11, request_id=REQUEST_ID) == 0


async def test_attempt_key_carries_tenant_lead_and_note(
    fake: FakeOperationalRedis,
) -> None:
    await _take()
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
    await _take()
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


async def test_take_prompt_slots_fails_open_with_both_guards_lines(
    failing, json_log
) -> None:
    """An unreachable store costs a question too many, never a refusal: the take is
    granted on the attempts read earlier, and each guard logs its own bypass."""
    failing("eval")
    assert await _take(cap=2, attempts_read=1) == (1, True, 0)

    codes = [line["reason_code"] for line in _lines(json_log)]
    assert codes == ["attempt_counter_bypassed", "rate_limit_bypassed"]


async def test_read_attempts_fails_open_with_a_log_line(failing, json_log) -> None:
    failing("get")
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 0

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
    failing("set", "get", "eval")
    with pytest.raises(IdempotencyUnavailableError):
        await reserve_idempotency(
            TENANT, NOTE_ID, SENTINEL_FINGERPRINT, ttl=IDEM_TTL, request_id=REQUEST_ID
        )
    assert await read_rate_limit(TENANT, SUBJECT, request_id=REQUEST_ID) == 0
    assert await read_attempts(TENANT, LEAD_ID, NOTE_ID, request_id=REQUEST_ID) == 0
    assert await _take() == (0, True, 0)


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
