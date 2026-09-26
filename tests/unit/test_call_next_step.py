"""The next step's kind, time and booking (passes.py): the time the model
resolves from the call's recorded_at and the tenant's zone, both in the data
half, and code's word on it -- held to the call, in the tenant's zone."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.validation import OutputValidationError
from dodeal_ai.units.call_intelligence.analysis import call_clock
from dodeal_ai.units.call_intelligence.config import CallsConfig
from dodeal_ai.units.call_intelligence.passes import (
    NOT_MENTIONED,
    STATED,
    UNCERTAIN,
    CallClock,
    Extraction,
    check_extraction,
    extract,
    settled,
)
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_passes import _call, _extraction

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")

DUBAI = ZoneInfo("Asia/Dubai")
# The call was recorded on 2026-09-26 at ten in the morning, Dubai time.
RECORDED = datetime(2026, 9, 26, 10, 0, tzinfo=DUBAI)
CLOCK = CallClock(RECORDED, "Asia/Dubai")


def _step(**given: Any) -> dict[str, Any]:
    answer = _extraction()
    answer["next_step"].update(given)
    return answer


def _kept(answer: dict[str, Any], clock: CallClock = CLOCK) -> dict[str, Any]:
    return settled(Extraction.model_validate(answer), _call(), clock)["next_step"]


# --- the guards --------------------------------------------------------------------


async def test_tomorrow_at_5_on_a_call_of_the_26th_is_the_27th_at_17_dubai() -> None:
    """The guard: the model reads the call's date and the zone from the data
    half; code keeps its answer as the tenant's local time."""
    answer = _step(due="tomorrow at 5", when="2026-09-27T17:00:00+04:00", booked=True)
    llm = FakeLLM(json_response(answer))

    found, _ = await extract(
        llm, _call(), scope=SCOPE, settings=get_settings(), clock=CLOCK
    )

    (sent,) = llm.calls
    assert sent.prompt.variable.endswith(
        "RECORDED AT: 2026-09-26T10:00:00+04:00\nTIMEZONE: Asia/Dubai\n"
        "----- END CALLER DATA -----"
    )
    kept = settled(found, _call(), CLOCK)["next_step"]
    assert (kept["when"], kept["when_state"]) == ("2026-09-27T17:00:00+04:00", STATED)
    assert (kept["kind"], kept["booked"]) == ("viewing", True)


def test_next_week_sometime_gives_no_time_and_an_uncertain_one() -> None:
    """The guard: vague timing is never made a time."""
    kept = _kept(_step(due="next week sometime", when=None, booked=False))
    assert (kept["when"], kept["when_state"], kept["booked"]) == (
        None,
        UNCERTAIN,
        False,
    )


# --- held to the call --------------------------------------------------------------


def test_a_time_given_in_utc_is_delivered_in_the_tenants_zone() -> None:
    kept = _kept(_step(when="2026-09-27T13:00:00Z", booked=True))
    assert kept["when"] == "2026-09-27T17:00:00+04:00"


@pytest.mark.parametrize(
    "when",
    [
        "2026-09-26T09:59:00+04:00",
        "2026-09-20T17:00:00+04:00",
        "2026-12-25T10:00:01+04:00",
        "2027-06-01T10:00:00+04:00",
    ],
    ids=["a-minute-before", "last-week", "past-90-days", "next-year"],
)
def test_a_time_before_the_call_or_past_90_days_is_refused(when: str) -> None:
    kept = _kept(_step(when=when, booked=True))
    assert (kept["when"], kept["when_state"], kept["booked"]) == (
        None,
        UNCERTAIN,
        False,
    )


@pytest.mark.parametrize(
    "when", ["2026-09-26T10:00:00+04:00", "2026-12-25T10:00:00+04:00"]
)
def test_the_call_itself_and_the_90th_day_are_kept(when: str) -> None:
    assert _kept(_step(when=when))["when"] == when


def test_booked_needs_a_held_time_and_verified_evidence() -> None:
    """Both sides agreed a time: a booking with no time, or resting on a quote
    that failed, is not one."""
    assert _kept(_step(when=None, booked=True))["booked"] is False
    unverified = _kept(
        _step(when="2026-09-27T17:00:00+04:00", booked=True, quote="we never met")
    )
    assert (unverified["unverified"], unverified["booked"]) == (True, False)
    assert (unverified["when"], unverified["when_state"]) == (
        "2026-09-27T17:00:00+04:00",
        UNCERTAIN,
    )


def test_no_time_said_is_not_mentioned() -> None:
    kept = _kept(_step(due=None, when=None))
    assert (kept["when"], kept["when_state"]) == (None, NOT_MENTIONED)


def test_with_the_calls_time_unknown_no_time_is_held() -> None:
    kept = _kept(_step(when="2026-09-27T17:00:00+04:00"), CallClock(None, "UTC"))
    assert (kept["when"], kept["when_state"]) == (None, UNCERTAIN)


# --- the shape ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "error"),
    [
        ({"kind": None}, "action_without_kind"),
        (
            {"action": None, "quote": None, "segment": None, "kind": "viewing"},
            "next_step_without_action",
        ),
        (
            {
                "action": None,
                "quote": None,
                "segment": None,
                "kind": None,
                "booked": True,
            },
            "next_step_without_action",
        ),
    ],
    ids=["no-kind", "kind-alone", "booked-alone"],
)
def test_a_kind_goes_with_an_action_and_only_with_one(
    given: dict[str, Any], error: str
) -> None:
    answer = Extraction.model_validate(_step(**given))
    with pytest.raises(OutputValidationError) as refused:
        check_extraction(_call())(answer)
    assert refused.value.errors == (("next_step", error),)


@pytest.mark.parametrize("when", ["2026-09-27T17:00:00", "tomorrow at 5", "2026-09-27"])
def test_a_time_without_its_offset_or_not_iso_is_refused_by_the_schema(
    when: str,
) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Extraction.model_validate(_step(when=when))


@pytest.mark.parametrize(
    "kind",
    ["viewing", "online_meeting", "office_visit", "callback", "send_details", "other"],
)
def test_each_kind_is_accepted(kind: str) -> None:
    assert _kept(_step(kind=kind))["kind"] == kind


# --- the clock ---------------------------------------------------------------------


def _job(recorded_at: object) -> Any:
    """A job as call_clock reads it: its metadata only."""
    metadata: dict[str, object] = {"duration_seconds": 150}
    if recorded_at is not None:
        metadata["recorded_at"] = recorded_at
    return SimpleNamespace(metadata=metadata)


@pytest.mark.parametrize(
    ("recorded_at", "expected"),
    [
        (
            "2026-09-26T06:00:00+00:00",
            datetime(2026, 9, 26, 6, 0, tzinfo=ZoneInfo("UTC")),
        ),
        ("2026-09-26T06:00:00", None),
        ("not a time", None),
        (None, None),
    ],
)
def test_the_clock_reads_the_pushed_recorded_at_and_the_tenants_zone(
    recorded_at: object, expected: datetime | None
) -> None:
    clock = call_clock(_job(recorded_at), CallsConfig(timezone="Asia/Riyadh"))
    assert (clock.recorded_at, clock.timezone) == (expected, "Asia/Riyadh")


def test_the_tenants_zone_defaults_to_dubai_and_refuses_an_unknown_one() -> None:
    from pydantic import ValidationError

    assert CallsConfig().timezone == "Asia/Dubai"
    with pytest.raises(ValidationError):
        CallsConfig(timezone="Mars/Olympus")
