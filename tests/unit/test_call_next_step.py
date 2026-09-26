"""The next step's kind, time and booking (passes.py): the time the model
resolves from each segment's said-at stamp, written in code in the data half,
and code's word on it -- the words that name it quoted and checked, held to
the moment they were said, to the minute, in the tenant's zone."""

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
from dodeal_ai.units.call_intelligence.evidence import CallText
from dodeal_ai.units.call_intelligence.passes import (
    NOT_MENTIONED,
    STATED,
    UNCERTAIN,
    WHEN_MISSING_QUOTE,
    WHEN_NO_ANCHOR,
    WHEN_OUT_OF_RANGE,
    CallClock,
    Extraction,
    check_extraction,
    extract,
    extraction_quotes,
    settled,
)
from dodeal_ai.units.call_intelligence.transcriber import Segment, Transcript
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.unit.test_call_passes import _call, _extraction

SCOPE = RequestContext.for_admitted_job(
    "tenant-a", request_id="req-1"
).scope_for_author(27, budget="calls")

DUBAI = ZoneInfo("Asia/Dubai")
# The call was recorded on 2026-09-26 at ten in the morning, Dubai time.
RECORDED = datetime(2026, 9, 26, 10, 0, tzinfo=DUBAI)
CLOCK = CallClock(RECORDED, "Asia/Dubai")


# The default call's time words: s3, said 10 s in, so at 10:00:10 Dubai.
TUESDAY = {"when_quote": "Tuesday at four", "when_segment": "s3"}


def _step(**given: Any) -> dict[str, Any]:
    answer = _extraction()
    answer["next_step"].update(given)
    return answer


def _timed(when: str, **given: Any) -> dict[str, Any]:
    """A step with a time, quoting the words that name it (TUESDAY)."""
    return _step(when=when, **{**TUESDAY, **given})


def _kept(answer: dict[str, Any], clock: CallClock = CLOCK) -> dict[str, Any]:
    return settled(Extraction.model_validate(answer), _call(), clock)["next_step"]


# --- the guards --------------------------------------------------------------------


async def test_tomorrow_at_5_on_a_call_of_the_26th_is_the_27th_at_17_dubai() -> None:
    """The model reads each segment's said-at stamp and the zone from the
    data half; code keeps its answer as the tenant's local time."""
    answer = _timed("2026-09-27T17:00:00+04:00", due="tomorrow at 5", booked=True)
    llm = FakeLLM(json_response(answer))

    found, _ = await extract(
        llm, _call(), scope=SCOPE, settings=get_settings(), clock=CLOCK
    )

    (sent,) = llm.calls
    assert sent.prompt.variable.endswith(
        "RECORDED AT: 2026-09-26T10:00:00+04:00\nTIMEZONE: Asia/Dubai\n"
        "----- END CALLER DATA -----"
    )
    assert (
        "[s3 00:10 agent @ 2026-09-26 10:00 Saturday] Shall we book the viewing"
        in sent.prompt.variable
    )
    kept = settled(found, _call(), CLOCK)["next_step"]
    assert (kept["when"], kept["when_state"]) == ("2026-09-27T17:00:00+04:00", STATED)
    assert (kept["kind"], kept["booked"], kept["when_reason"]) == (
        "viewing",
        True,
        None,
    )
    assert (kept["when_quote"], kept["when_segment"]) == ("Tuesday at four", "s3")


def test_next_week_sometime_gives_no_time_and_an_uncertain_one() -> None:
    """The guard: vague timing is never made a time."""
    kept = _kept(_step(due="next week sometime", when=None, booked=False))
    assert (kept["when"], kept["when_state"], kept["booked"]) == (
        None,
        UNCERTAIN,
        False,
    )
    assert kept["when_reason"] is None


# --- held to the call --------------------------------------------------------------


def test_a_time_given_in_utc_is_delivered_in_the_tenants_zone() -> None:
    kept = _kept(_timed("2026-09-27T13:00:00Z", booked=True))
    assert kept["when"] == "2026-09-27T17:00:00+04:00"


@pytest.mark.parametrize(
    "when",
    [
        "2026-09-26T09:59:09+04:00",
        "2026-09-20T17:00:00+04:00",
        "2027-09-26T10:00:11+04:00",
        "2028-01-01T10:00:00+04:00",
    ],
    ids=["61-s-before", "last-week", "past-365-days", "in-2028"],
)
def test_a_time_out_of_range_of_the_moment_is_dropped_booked_unchanged(
    when: str,
) -> None:
    """The guard: before the moment its words were said (s3, 10:00:10) less
    60 s, or more than 365 days after it, is no time; booked is not changed."""
    kept = _kept(_timed(when, booked=True))
    assert (kept["when"], kept["when_state"], kept["when_reason"]) == (
        None,
        UNCERTAIN,
        WHEN_OUT_OF_RANGE,
    )
    assert (kept["booked"], kept["when_quote"]) == (True, "Tuesday at four")


@pytest.mark.parametrize(
    ("when", "kept_as"),
    [
        ("2026-09-26T09:59:10+04:00", "2026-09-26T09:59:00+04:00"),
        ("2027-09-26T10:00:10+04:00", "2027-09-26T10:00:00+04:00"),
    ],
    ids=["60-s-before", "the-365th-day"],
)
def test_60_s_before_the_moment_and_its_365th_day_are_kept(
    when: str, kept_as: str
) -> None:
    kept = _kept(_timed(when))
    assert (kept["when"], kept["when_state"]) == (kept_as, STATED)


def test_a_callback_4_months_on_is_kept() -> None:
    """A year is the range: a callback "بعد 4 شهور" is a plan, not a slip."""
    kept = _kept(_timed("2027-01-26T10:00:00+04:00", booked=True))
    assert (kept["when"], kept["when_state"], kept["booked"]) == (
        "2027-01-26T10:00:00+04:00",
        STATED,
        True,
    )


def test_seconds_are_dropped_to_the_minute() -> None:
    kept = _kept(_timed("2026-09-27T17:00:47.250+04:00"))
    assert kept["when"] == "2026-09-27T17:00:00+04:00"


@pytest.mark.parametrize(
    "given",
    [
        {"when_quote": None, "when_segment": None},
        {"when_quote": "Tuesday at four", "when_segment": None},
        {"when_quote": "Wednesday at noon", "when_segment": "s3"},
        {"when_quote": "Tuesday at four", "when_segment": "s1"},
    ],
    ids=["no-quote", "no-segment", "not-said", "not-in-segment-or-neighbour"],
)
def test_a_time_without_a_verified_when_quote_is_dropped_booked_unchanged(
    given: dict[str, Any],
) -> None:
    """The guard: the model's time stands only on the words that name it."""
    kept = _kept(_step(when="2026-09-27T17:00:00+04:00", booked=True, **given))
    assert (kept["when"], kept["when_state"], kept["when_reason"]) == (
        None,
        UNCERTAIN,
        WHEN_MISSING_QUOTE,
    )
    assert (kept["booked"], kept["unverified"]) == (True, False)
    assert (kept["when_quote"], kept["when_segment"]) == (None, None)


def test_the_when_quote_is_checked_among_the_extractions_quotes() -> None:
    answer = Extraction.model_validate(_step(when="2026-09-27T17:00:00+04:00"))
    assert extraction_quotes(_call(), answer)["next_step.when"] == [
        ("next_step.when", "quote_missing")
    ]
    words_only = Extraction.model_validate(_step(**TUESDAY))
    assert extraction_quotes(_call(), words_only)["next_step.when"] == []


def test_booked_needs_a_time_given_and_a_verified_agreement() -> None:
    """Both sides agreed a time: a booking with no time, or resting on an
    agreement quote that failed, is not one; the time itself stands on its own
    quote but is uncertain under an unverified agreement."""
    assert _kept(_step(when=None, booked=True))["booked"] is False
    unverified = _kept(
        _timed("2026-09-27T17:00:00+04:00", booked=True, quote="we never met")
    )
    assert (unverified["unverified"], unverified["booked"]) == (True, False)
    assert (unverified["when"], unverified["when_state"]) == (
        "2026-09-27T17:00:00+04:00",
        UNCERTAIN,
    )


def test_no_time_said_is_not_mentioned() -> None:
    kept = _kept(_step(due=None, when=None))
    assert (kept["when"], kept["when_state"], kept["when_reason"]) == (
        None,
        NOT_MENTIONED,
        None,
    )


async def test_with_the_calls_time_unknown_no_stamp_is_written_nor_time_held() -> None:
    """The guard: with no recorded_at there is no moment to hold a time to,
    so the model's time is dropped with when_no_anchor, booked unchanged."""
    unknown = CallClock(None, "UTC")
    llm = FakeLLM(json_response(_timed("2026-09-27T17:00:00+04:00", booked=True)))

    found, _ = await extract(
        llm, _call(), scope=SCOPE, settings=get_settings(), clock=unknown
    )

    assert "@" not in llm.calls[0].prompt.variable
    kept = settled(found, _call(), unknown)["next_step"]
    assert (kept["when"], kept["when_state"], kept["when_reason"]) == (
        None,
        UNCERTAIN,
        WHEN_NO_ANCHOR,
    )
    assert (kept["booked"], kept["when_quote"]) == (True, "Tuesday at four")


def test_an_unknown_call_time_is_no_anchor_even_without_a_when_quote() -> None:
    """recorded_at unknown decides the reason before the time's quote does."""
    kept = _kept(
        _step(when="2026-09-27T17:00:00+04:00", booked=True),
        CallClock(None, "Asia/Dubai"),
    )
    assert (kept["when"], kept["when_state"], kept["when_reason"]) == (
        None,
        UNCERTAIN,
        WHEN_NO_ANCHOR,
    )
    assert kept["booked"] is True


@pytest.mark.parametrize(
    ("answer", "clock", "reason"),
    [
        (_step(when="2026-09-27T17:00:00+04:00"), CLOCK, WHEN_MISSING_QUOTE),
        (_timed("2026-09-20T17:00:00+04:00"), CLOCK, WHEN_OUT_OF_RANGE),
        (
            _timed("2026-09-27T17:00:00+04:00"),
            CallClock(None, "Asia/Dubai"),
            WHEN_NO_ANCHOR,
        ),
    ],
    ids=["missing-quote", "out-of-range", "no-anchor"],
)
@pytest.mark.parametrize("model_booked", [True, False])
def test_a_time_code_dropped_leaves_booked_as_the_model_gave_it(
    answer: dict[str, Any], clock: CallClock, reason: str, model_booked: bool
) -> None:
    """The guard: booked follows the model's own time, never code's held one;
    each reason code drops a time for leaves the model's booked as it was."""
    answer = {**answer, "next_step": {**answer["next_step"], "booked": model_booked}}
    kept = _kept(answer, clock)
    assert (kept["when"], kept["when_reason"]) == (None, reason)
    assert kept["booked"] is model_booked


def test_no_time_from_the_model_is_never_booked_whatever_the_clock() -> None:
    """The one rule that sets booked false on its own: no time given at all."""
    for clock in (CLOCK, CallClock(None, "Asia/Dubai")):
        kept = _kept(_step(when=None, booked=True, **TUESDAY), clock)
        assert (kept["when"], kept["when_reason"], kept["booked"]) == (
            None,
            None,
            False,
        )


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
        (
            {"action": None, "quote": None, "segment": None, "kind": None, **TUESDAY},
            "next_step_without_action",
        ),
    ],
    ids=["no-kind", "kind-alone", "booked-alone", "time-words-alone"],
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


# --- a time from the moment its words were said ------------------------------------


def _ar(start: float, speaker: str, text: str) -> Segment:
    return Segment(
        start_s=start,
        end_s=start + 5,
        speaker=speaker,
        text=text,
        language="ar",
        confidence=0.9,
    )


def _arabic_call(*segments: Segment) -> CallText:
    return CallText.of(
        Transcript.of(segments, provider="fake", model="fake"), country_code="971"
    )


GREETING = _ar(0, "agent", "مرحبا، معك مكتب المبيعات بخصوص الشقة")
IN_3_MINUTES = _ar(300, "agent", "تمام، بتصل عليك بعد 3 دقايق عشان نكمل")
OKAY = _ar(310, "lead", "اوكي")


def _callback(
    when: str, *, quote: str, segment: str, when_segment: str = "s2"
) -> dict[str, Any]:
    """An answer whose next step is a callback: the agreement quoted at
    `quote`/`segment`, the time's words "بعد 3 دقايق" at `when_segment`."""
    answer = _extraction()
    answer.update(
        wanted=None,
        discussed=[],
        agreed=[],
        mood={"value": "positive", "quote": "اوكي", "segment": "s3"},
        next_step={
            "action": "Call the client back",
            "owner": "agent",
            "due": "بعد 3 دقايق",
            "quote": quote,
            "segment": segment,
            "kind": "callback",
            "when": when,
            "booked": True,
            "when_quote": "بعد 3 دقايق",
            "when_segment": when_segment,
        },
    )
    return answer


async def _extracted(
    call: CallText, answer: dict[str, Any], clock: CallClock = CLOCK
) -> tuple[dict[str, Any], str]:
    """The settled next step, and the data half the model was sent."""
    llm = FakeLLM(json_response(answer))
    found, _ = await extract(
        llm, call, scope=SCOPE, settings=get_settings(), clock=clock
    )
    return settled(found, call, clock)["next_step"], llm.calls[0].prompt.variable


async def test_3_minutes_said_at_05_00_and_agreed_at_05_10_is_due_at_10_08() -> None:
    """The guard: "بعد 3 دقايق" said at 05:00 on a call recorded at 10:00 is
    stamped 10:05 in code, and the client's "اوكي" at 05:10 books 10:08."""
    call = _arabic_call(GREETING, IN_3_MINUTES, OKAY)
    answer = _callback("2026-09-26T10:08:00+04:00", quote="اوكي", segment="s3")

    step, sent = await _extracted(call, answer)

    assert (
        "[s2 05:00 agent @ 2026-09-26 10:05 Saturday] تمام، بتصل عليك بعد 3 دقايق"
        in sent
    )
    assert "[s3 05:10 client @ 2026-09-26 10:05 Saturday] اوكي" in sent
    assert (step["when"], step["when_state"], step["booked"]) == (
        "2026-09-26T10:08:00+04:00",
        STATED,
        True,
    )
    assert (step["when_segment"], step["segment"], step["when_reason"]) == (
        "s2",
        "s3",
        None,
    )


@pytest.mark.parametrize("recap_s", [540, 600], ids=["recap-09-00", "recap-10-00"])
async def test_a_recap_quoting_the_agreement_still_anchors_on_05_00(
    recap_s: int,
) -> None:
    """The guard: the agreement quote sits in a later recap; the anchor is
    where the time was named (05:00), never the agreement's segment."""
    recap = _ar(recap_s, "agent", "زي ما اتفقنا، اوكي، بكلمك")
    call = _arabic_call(GREETING, IN_3_MINUTES, OKAY, recap)
    answer = _callback(
        "2026-09-26T10:08:00+04:00", quote="زي ما اتفقنا، اوكي", segment="s4"
    )

    step, _ = await _extracted(call, answer)

    assert (step["when"], step["when_state"], step["booked"]) == (
        "2026-09-26T10:08:00+04:00",
        STATED,
        True,
    )


async def test_a_time_counted_from_the_calls_start_is_dropped() -> None:
    """The guard: 3 minutes counted from recorded_at (10:03), not from the
    moment "بعد 3 دقايق" was said (10:05), is out of range."""
    call = _arabic_call(GREETING, IN_3_MINUTES, OKAY)
    answer = _callback("2026-09-26T10:03:00+04:00", quote="اوكي", segment="s3")

    step, _ = await _extracted(call, answer)

    assert (step["when"], step["when_reason"], step["booked"]) == (
        None,
        WHEN_OUT_OF_RANGE,
        True,
    )


async def test_tomorrow_said_after_local_midnight_is_the_day_after_that() -> None:
    """The guard: a call that began at 23:55 says "بكرة" ten minutes in, on
    the 27th; its stamp carries the 27th, and the 28th at 17:00 is kept."""
    late = CallClock(datetime(2026, 9, 26, 23, 55, tzinfo=DUBAI), "Asia/Dubai")
    tomorrow = _ar(600, "agent", "بكرة الساعة 5 بمر عليك")
    call = _arabic_call(GREETING, tomorrow, _ar(606, "lead", "اوكي"))
    answer = _callback(
        "2026-09-28T17:00:00+04:00", quote="اوكي", segment="s3", when_segment="s2"
    )
    answer["next_step"].update(when_quote="بكرة الساعة 5", due="بكرة الساعة 5")

    step, sent = await _extracted(call, answer, late)

    assert "[s1 00:00 agent @ 2026-09-26 23:55 Saturday]" in sent
    assert "[s2 10:00 agent @ 2026-09-27 00:05 Sunday] بكرة الساعة 5" in sent
    assert (step["when"], step["when_state"]) == ("2026-09-28T17:00:00+04:00", STATED)


async def test_a_when_quote_found_in_the_next_segment_anchors_on_that_one() -> None:
    """The guard: cited in s2 (00:10), found in s3 (05:00): the found segment
    is stored and anchors the time, so 10:08 is kept and 10:03 dropped."""
    check = _ar(10, "agent", "خليني اتأكد من الموعد")
    said, okay = _ar(300, "agent", "بتصل عليك بعد 3 دقايق"), _ar(305, "lead", "اوكي")
    call = _arabic_call(GREETING, check, said, okay)

    def cited(when: str) -> dict[str, Any]:
        answer = _callback(when, quote="اوكي", segment="s4", when_segment="s2")
        answer["mood"]["segment"] = "s4"
        return answer

    kept, _ = await _extracted(call, cited("2026-09-26T10:08:00+04:00"))
    early, _ = await _extracted(call, cited("2026-09-26T10:03:00+04:00"))

    assert (kept["when"], kept["when_segment"]) == ("2026-09-26T10:08:00+04:00", "s3")
    assert (early["when"], early["when_reason"]) == (None, WHEN_OUT_OF_RANGE)


async def test_a_dubai_recording_for_a_cairo_company_is_stamped_in_cairo() -> None:
    """The guard: recorded_at given in Dubai's offset; the company's zone is
    Africa/Cairo (+03:00 in September), so the stamps and the time kept are
    Cairo's, whichever offset the model wrote."""
    cairo = CallClock(RECORDED, "Africa/Cairo")
    call = _arabic_call(GREETING, IN_3_MINUTES, OKAY)
    given = ("2026-09-26T09:08:00+03:00", "2026-09-26T10:08:00+04:00")

    kept = [
        (await _extracted(call, _callback(when, quote="اوكي", segment="s3"), cairo))
        for when in given
    ]

    assert "[s2 05:00 agent @ 2026-09-26 09:05 Saturday]" in kept[0][1]
    assert (
        "RECORDED AT: 2026-09-26T09:00:00+03:00\nTIMEZONE: Africa/Cairo" in (kept[0][1])
    )
    assert [step["when"] for step, _ in kept] == ["2026-09-26T09:08:00+03:00"] * 2


def test_the_extract_prompt_resolves_a_time_from_the_said_at_stamp() -> None:
    from dodeal_ai.core.prompting import build_prompt
    from dodeal_ai.units.call_intelligence.prompts import EXTRACT_TEMPLATE

    text = " ".join(build_prompt(EXTRACT_TEMPLATE, caller_data="").stable.split())
    assert "[s<n> mm:ss speaker @ YYYY-MM-DD HH:MM Weekday] text" in text
    assert "never work it out again from RECORDED AT and mm:ss" in text
    assert (
        '"بعد 3 دقايق" on a line stamped "2026-09-26 10:05 Saturday" is '
        "2026-09-26 at 10:08"
    ) in text
    assert "tomorrow from the stamp's date, not the call's first date" in text
    for field in ('"when_quote": string or null', '"when_segment": string or null'):
        assert field in text
    assert "A when without a when_quote is dropped." in text
    for agreement in ('"اوكي"', '"تمام"'):
        assert agreement in text
