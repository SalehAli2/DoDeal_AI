"""Register item 89: the backend read retries once only on a transient failure,
and a 4xx leaves as a typed error that is never retried."""

from __future__ import annotations

import asyncio
import io
import json
import logging

import httpx
import pytest

from dodeal_ai.core import resilience
from dodeal_ai.core.config import Settings
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.tools.errors import (
    BackendForbidden,
    BackendNotFound,
    BackendRejected,
    BackendStatusError,
    BackendUnauthorized,
    retry_after_seconds,
)
from dodeal_ai.tools.keys import SettingsKeyResolver
from dodeal_ai.tools.leads import (
    GET_LEAD_LABEL,
    RETRY_AFTER_CAP_SECONDS,
    RETRY_JITTER_MAX_SECONDS,
    RETRY_JITTER_MIN_SECONDS,
    LeadsClient,
    _jitter,
    backend_retry_delay,
)
from tests.helpers.scopes import TEST_SCOPE

JITTER = 0.25
LEAD_BODY = {"status": True, "data": {"id": 7}}
KEY = "key-tenant-a-secret"


class ScriptedTransport:
    """Answers each call with the next scripted body or exception."""

    def __init__(self, *outcomes: object) -> None:
        self._outcomes = list(outcomes)
        self.calls = 0

    async def get_json(self, url: str, headers: dict[str, str]) -> object:
        self.calls += 1
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    """Every wait the watchdog schedules, recorded instead of slept."""
    recorded: list[float] = []

    async def _record(delay: float) -> None:
        recorded.append(delay)

    monkeypatch.setattr(resilience.asyncio, "sleep", _record)
    return recorded


@pytest.fixture
def json_log():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(x) for x in stream.getvalue().splitlines()]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _client(transport: ScriptedTransport) -> LeadsClient:
    settings = Settings(
        _env_file=None, jwt_signing_key="test-key", dd_api_keys={"tenant-a": KEY}
    )
    return LeadsClient(
        transport, SettingsKeyResolver(settings), settings, jitter=lambda: JITTER
    )


# --- the rule ---------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("slow connect"),
        httpx.ReadTimeout("slow read"),
        TimeoutError(),
        BackendStatusError(500),
        BackendStatusError(502),
        BackendStatusError(503),
        BackendStatusError(429),
    ],
    ids=["connect", "connect-timeout", "read-timeout", "timeout", "500", "502", "503"]
    + ["429"],
)
def test_a_transient_failure_waits_the_jitter(exc):
    """Connect error, read timeout, 5xx and a 429 with no Retry-After retry after jitter."""
    assert backend_retry_delay(exc, lambda: JITTER) == JITTER


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422])
def test_a_4xx_other_than_429_is_never_retried(status):
    """Every other 4xx has no retry at all."""
    assert backend_retry_delay(BackendStatusError(status), lambda: JITTER) is None


@pytest.mark.parametrize(
    "exc", [RuntimeError("bug"), httpx.ReadError("reset"), httpx.PoolTimeout("pool")]
)
def test_a_failure_not_on_the_list_is_never_retried(exc):
    """Anything outside the four named failures fails closed without a retry."""
    assert backend_retry_delay(exc, lambda: JITTER) is None


@pytest.mark.parametrize("seconds", [0.0, 1.5, RETRY_AFTER_CAP_SECONDS])
def test_a_429_waits_its_retry_after_up_to_the_cap(seconds):
    """A Retry-After at or under two seconds is the wait."""
    exc = BackendStatusError(429, retry_after=seconds)
    assert backend_retry_delay(exc, lambda: JITTER) == seconds


def test_a_429_asking_for_more_than_the_cap_is_not_retried():
    """A Retry-After past two seconds fails closed instead of retrying early."""
    exc = BackendStatusError(429, retry_after=RETRY_AFTER_CAP_SECONDS + 0.5)
    assert backend_retry_delay(exc, lambda: JITTER) is None


def test_the_default_jitter_stays_inside_100_to_400_ms():
    """A thousand draws all land inside the band."""
    draws = [_jitter() for _ in range(1000)]
    assert min(draws) >= RETRY_JITTER_MIN_SECONDS
    assert max(draws) <= RETRY_JITTER_MAX_SECONDS
    assert len(set(draws)) > 1


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("1", 1.0),
        ("1.5", 1.5),
        (None, None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("-1", None),
        ("nan", None),
        ("inf", None),
    ],
)
def test_retry_after_is_read_in_seconds_only(header, seconds):
    """The delta-seconds form parses; a date, a negative and junk read as absent."""
    assert retry_after_seconds(header) == seconds


# --- the client ---------------------------------------------------------------


async def test_a_503_then_a_200_is_one_retry_after_the_jitter(sleeps):
    """A 5xx retries once after the jitter and returns the second answer."""
    transport = ScriptedTransport(BackendStatusError(503), LEAD_BODY)
    lead = await _client(transport).get_lead(TEST_SCOPE, 7)
    assert lead.id == 7
    assert transport.calls == 2
    assert sleeps == [JITTER]


async def test_a_429_waits_what_the_backend_asked(sleeps):
    """A 429's Retry-After is the wait before its retry."""
    transport = ScriptedTransport(BackendStatusError(429, retry_after=1.0), LEAD_BODY)
    await _client(transport).get_lead(TEST_SCOPE, 7)
    assert sleeps == [1.0]


async def test_two_503s_fail_closed_after_two_calls(sleeps):
    """The retry is once: a second 5xx is ExternalCallError."""
    transport = ScriptedTransport(BackendStatusError(503), BackendStatusError(503))
    with pytest.raises(ExternalCallError):
        await _client(transport).get_lead(TEST_SCOPE, 7)
    assert transport.calls == 2


async def test_two_connect_errors_fail_closed_after_two_calls(sleeps):
    """A dead host is retried once, then ExternalCallError."""
    transport = ScriptedTransport(httpx.ConnectError("x"), httpx.ConnectError("x"))
    with pytest.raises(ExternalCallError):
        await _client(transport).get_lead(TEST_SCOPE, 7)
    assert transport.calls == 2


async def test_a_429_asking_too_long_is_one_call(sleeps):
    """A long Retry-After fails closed on the first answer."""
    transport = ScriptedTransport(BackendStatusError(429, retry_after=30.0))
    with pytest.raises(ExternalCallError):
        await _client(transport).get_lead(TEST_SCOPE, 7)
    assert transport.calls == 1
    assert sleeps == []


@pytest.mark.parametrize(
    ("status", "error"),
    [
        (401, BackendUnauthorized),
        (403, BackendForbidden),
        (404, BackendNotFound),
        (400, BackendRejected),
        (422, BackendRejected),
    ],
)
async def test_a_4xx_is_its_typed_error_after_one_call(sleeps, status, error):
    """401, 403, 404 and other 4xx each leave as their own type, unretried."""
    transport = ScriptedTransport(BackendStatusError(status))
    with pytest.raises(error) as caught:
        await _client(transport).get_lead(TEST_SCOPE, 7)
    assert transport.calls == 1
    assert sleeps == []
    assert caught.value.label == GET_LEAD_LABEL
    assert caught.value.status == status
    assert str(caught.value) == error.reason_code


@pytest.mark.parametrize(
    ("status", "event"), [(401, "backend_unauthorized"), (403, "backend_forbidden")]
)
async def test_a_refused_key_logs_error_without_the_key(json_log, status, event):
    """401 and 403 log one ERROR with tenant, label and status, and never the key."""
    transport = ScriptedTransport(BackendStatusError(status))
    with pytest.raises((BackendUnauthorized, BackendForbidden)):
        await _client(transport).get_lead(TEST_SCOPE, 7)
    lines = [x for x in json_log() if x["message"] == event]
    assert len(lines) == 1
    assert lines[0]["level"] == "ERROR"
    assert lines[0]["tenant"] == "tenant-a"
    assert lines[0]["label"] == GET_LEAD_LABEL
    assert lines[0]["status"] == status
    assert KEY not in json.dumps(json_log())


async def test_a_404_logs_no_error(json_log):
    """Only the refused-key statuses log at ERROR from the tool layer."""
    transport = ScriptedTransport(BackendStatusError(404))
    with pytest.raises(BackendNotFound):
        await _client(transport).get_lead(TEST_SCOPE, 7)
    assert not [x for x in json_log() if x["level"] == "ERROR"]


async def test_no_retry_is_scheduled_past_the_deadline(sleeps, json_log):
    """A retry whose wait ends past the deadline is skipped and logged."""
    transport = ScriptedTransport(BackendStatusError(503), LEAD_BODY)
    deadline = asyncio.get_running_loop().time() + JITTER / 2
    with pytest.raises(ExternalCallError):
        await _client(transport).get_lead(TEST_SCOPE, 7, deadline=deadline)
    assert transport.calls == 1
    assert sleeps == []
    skipped = [x for x in json_log() if x["message"] == "external_call_retry_skipped"]
    assert skipped[0]["reason_code"] == "deadline"


async def test_a_retry_inside_the_deadline_is_taken(sleeps):
    """A deadline far enough away leaves the retry alone."""
    transport = ScriptedTransport(BackendStatusError(503), LEAD_BODY)
    deadline = asyncio.get_running_loop().time() + 60
    await _client(transport).get_lead(TEST_SCOPE, 7, deadline=deadline)
    assert transport.calls == 2


async def test_every_read_takes_the_deadline(sleeps):
    """get_leads and get_lead_notes pass the deadline through as get_lead does."""
    deadline = asyncio.get_running_loop().time() + JITTER / 2
    meta = {"current_page": 1, "per_page": 25, "total": 0, "last_page": 1}
    for method, args in (("get_leads", ()), ("get_lead_notes", (7,))):
        transport = ScriptedTransport(
            BackendStatusError(503), {"status": True, "data": [], "meta": meta}
        )
        with pytest.raises(ExternalCallError):
            await getattr(_client(transport), method)(
                TEST_SCOPE, *args, deadline=deadline
            )
        assert transport.calls == 1
