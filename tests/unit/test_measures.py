"""The three per-rep measures, and the evidence floor that governs all three.

WHAT IS BEING PINNED, and why each claim gets its own test:

  THE FLOOR IS NEVER A LOW NUMBER. Below `measure_evidence_floor` rows a
  measure is suppressed with a reason. The failure this prevents is not a
  crash: it is a manager reading "poor, 33% flagged" off three notes and
  acting on it. So the tests assert `band is None` and `percent is None`
  beside the reason, because a figure appearing next to a suppression is
  exactly the shape that would be rendered.

  EACH MEASURE APPLIES THE FLOOR TO ITS OWN DENOMINATOR. A rep with thirty
  notes and two prompts has an average and no improvement figure.

  A ZERO DENOMINATOR IS A DIFFERENT REASON from a short one. "Nothing was
  asked about you" and "not enough yet" are different things to tell someone.

Rows are built by `_row()` below. They are invented, and they carry no note
text -- there is no field for one.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from dodeal_ai.units.structured_intelligence.config import (
    TenantConfig,
    get_tenant_config,
    parse_unit_a_section,
)
from dodeal_ai.units.structured_intelligence.judgement_rows import JudgementRow
from dodeal_ai.units.structured_intelligence.measures import (
    MeasureName,
    MeasureSuppressed,
    average_band,
    flagged_share,
    improved_share,
    local_dates,
    measure_rep,
    rolling_window,
)
from dodeal_ai.units.structured_intelligence.schemas import Band

CONFIG = get_tenant_config("tenant-a")
AUTHOR = 501
START = datetime(2026, 9, 1, tzinfo=UTC)

# A floor of 3 keeps the tables in these tests small enough to read. The
# default of 10 is exercised on its own below.
SMALL_FLOOR = dataclasses.replace(CONFIG, measure_evidence_floor=3)


def _row(
    *,
    note_id: int = 1,
    total: int | None = 72,
    flagged: bool = False,
    prompt_sent: bool = False,
    note_type: str | None = "discovery",
    suppressed_reason: str | None = None,
    rubric_version: str = "note_rubric_v2",
    config_version: str = "tenant-cfg-default-4",
    minutes: int = 0,
) -> JudgementRow:
    """One invented row. `total` None means a suppressed judgement, and the
    band is derived from the total the way the pipeline derives it."""
    scored = total is not None and suppressed_reason is None
    return JudgementRow.model_validate(
        {
            "note_id": note_id,
            "lead_id": 2000 + note_id,
            "author_id": AUTHOR,
            "note_created_at": (START + timedelta(minutes=minutes)).isoformat(),
            "note_type": note_type,
            "band": CONFIG.band_for(total).value if scored else None,
            "total": total if scored else None,
            "denominator": 80 if scored else None,
            "suppressed_reason": None
            if scored
            else (suppressed_reason or "not_scorable"),
            "prompt_sent": prompt_sent,
            "enforcement_verdict": "flag" if flagged else "allow",
            "rubric_version": rubric_version,
            "prompt_version": "unit_a_prompts_v2",
            "model_version": "invented-model-1",
            "config_version": config_version,
        }
    )


def _rows(count: int, **kwargs: object) -> list[JudgementRow]:
    """`count` rows that differ only in their note id."""
    return [_row(note_id=index, **kwargs) for index in range(1, count + 1)]  # type: ignore[arg-type]


# --- the window -------------------------------------------------------------


def test_the_window_is_the_tenants_rolling_days() -> None:
    """The 30 lives in the config and nowhere else, so two callers cannot
    measure different periods and describe them the same way. Register item
    156: whole local days, ending at local midnight today (Asia/Dubai, +04)."""
    now = datetime(2026, 9, 30, 7, 30, tzinfo=UTC)
    since, until = rolling_window(now, CONFIG)
    assert until == datetime(2026, 9, 29, 20, 0, tzinfo=UTC)
    assert since == until - timedelta(days=30)


def test_a_tenant_may_shorten_the_window() -> None:
    config = dataclasses.replace(CONFIG, rolling_window_days=7)
    now = datetime(2026, 9, 30, tzinfo=UTC)
    since, until = rolling_window(now, config)
    assert until - since == timedelta(days=7)


# --- the evidence floor -----------------------------------------------------


@pytest.mark.parametrize("count", [1, 2])
def test_an_average_below_the_floor_is_suppressed_and_carries_no_band(
    count: int,
) -> None:
    """THE CLAIM THE PIECE EXISTS FOR: a rep with too few notes has no average,
    and is not given a low one instead."""
    measure = average_band(_rows(count, total=30), SMALL_FLOOR)
    assert measure.band is None
    assert measure.suppressed is MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    assert measure.notes == count


def test_an_average_at_the_floor_is_reported() -> None:
    """At the floor, not above it: the floor is a minimum, not a threshold to
    clear."""
    measure = average_band(_rows(3, total=30), SMALL_FLOOR)
    assert measure.suppressed is None
    assert measure.band is Band.POOR


def test_no_rows_at_all_is_a_different_reason_from_too_few() -> None:
    """A rep who wrote nothing and a rep who wrote two notes are told different
    things, because they are different situations."""
    assert (
        average_band([], SMALL_FLOOR).suppressed is MeasureSuppressed.NOTHING_TO_MEASURE
    )
    assert (
        average_band(_rows(1), SMALL_FLOOR).suppressed
        is MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    )


def test_the_default_floor_is_ten() -> None:
    """The tenant default, exercised rather than assumed: nine notes is not an
    average at the shipped configuration."""
    assert CONFIG.measure_evidence_floor == 10
    assert average_band(_rows(9), CONFIG).suppressed is (
        MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    )
    assert average_band(_rows(10), CONFIG).suppressed is None


@pytest.mark.parametrize("floor", [0, -1])
def test_a_config_may_not_turn_the_floor_off(floor: int) -> None:
    """A floor of 0 reports a measure over no rows at all, which is the exact
    thing the floor exists to refuse."""
    with pytest.raises(ValueError):
        parse_unit_a_section(
            {"config_version": "tenant-b-cfg-1", "measure_evidence_floor": floor}
        )


# --- the average band -------------------------------------------------------


def test_the_totals_are_averaged_and_the_band_derived_from_the_mean() -> None:
    """Not an average of band positions: the band comes from config.band_for,
    the one function that may produce one."""
    rows = [
        _row(note_id=1, total=39),
        _row(note_id=2, total=41),
        _row(note_id=3, total=40),
    ]
    # mean 40 -> fair, one point over the poor boundary.
    assert average_band(rows, SMALL_FLOOR).band is Band.FAIR


def test_the_mean_rounds_half_up_in_integers() -> None:
    """No float reaches a figure a manager is shown, for the same reason none
    reaches a total: 69.5 is 70, which is `good`, and it is 70 every run."""
    rows = [_row(note_id=1, total=69), _row(note_id=2, total=70)]
    assert average_band(
        rows, dataclasses.replace(CONFIG, measure_evidence_floor=2)
    ).band is (Band.GOOD)


def test_suppressed_rows_are_not_averaged_in_as_zeros() -> None:
    """A suppressed judgement carries no band and cannot be averaged into
    anything -- it leaves the denominator, it is not a zero."""
    rows = _rows(3, total=88) + _rows(5, total=None)
    measure = average_band(rows, SMALL_FLOOR)
    assert measure.notes == 3
    assert measure.band is Band.EXCELLENT


def test_rows_from_another_rubric_version_are_excluded_and_counted() -> None:
    """A total is a percentage of a denominator the rubric chose. Mixing two
    rubrics silently is worse than reporting that some rows were dropped."""
    old = [
        _row(note_id=index, total=20, rubric_version="note_rubric_v1")
        for index in (1, 2)
    ]
    new = [_row(note_id=index, total=90) for index in (3, 4, 5)]
    measure = average_band(old + new, SMALL_FLOOR)
    assert measure.excluded == 2
    assert measure.notes == 3
    assert measure.band is Band.EXCELLENT


def test_rows_from_another_config_version_are_excluded_too() -> None:
    old = [_row(note_id=1, total=20, config_version="tenant-cfg-default-3")]
    new = _rows(3, total=90)
    # The LAST scored row's pair wins, and these rows are in recorded order.
    measure = average_band(old + new, SMALL_FLOOR)
    assert (measure.excluded, measure.notes) == (1, 3)


def test_exclusions_can_push_a_measure_below_the_floor() -> None:
    """The honest outcome: four notes, three under an old rubric, is not an
    average of one note dressed up as an average of four."""
    old = [
        _row(note_id=index, total=20, rubric_version="note_rubric_v1")
        for index in (1, 2, 3)
    ]
    measure = average_band(old + [_row(note_id=4, total=90)], SMALL_FLOOR)
    assert measure.band is None
    assert measure.suppressed is MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    assert (measure.notes, measure.excluded) == (1, 3)


def test_a_different_model_version_does_not_fragment_the_average() -> None:
    """Deliberately not part of the comparability pair: the model changes what
    was answered, which is the noise the average absorbs. Fragmenting on it
    would suppress every measure the day a provider rolls a point release."""
    rows = _rows(3, total=90)
    mixed = [
        JudgementRow.model_validate(
            row.model_dump(mode="json") | {"model_version": f"invented-model-{index}"}
        )
        for index, row in enumerate(rows)
    ]
    assert average_band(mixed, SMALL_FLOOR).excluded == 0
    assert average_band(mixed, SMALL_FLOOR).band is Band.EXCELLENT


# --- the flagged share ------------------------------------------------------


def test_the_flagged_share_counts_the_verdict_over_every_note() -> None:
    rows = _rows(3, flagged=True) + [_row(note_id=index) for index in (4, 5, 6, 7)]
    measure = flagged_share(rows, SMALL_FLOOR)
    assert measure.name is MeasureName.FLAGGED_SHARE
    assert (measure.counted, measure.of) == (3, 7)
    assert measure.percent == 43  # 42.857 -> 43, round half up in integers


def test_a_suppressed_note_is_still_one_of_the_reps_notes() -> None:
    """A note below the length floor is flagged and is theirs to fix; leaving
    suppressed rows out would hide the very notes this measure is about."""
    rows = [
        _row(note_id=1, total=None, flagged=True, note_type=None),
        _row(note_id=2, total=90),
        _row(note_id=3, total=90),
    ]
    measure = flagged_share(rows, SMALL_FLOOR)
    assert (measure.counted, measure.of, measure.percent) == (1, 3, 33)


def test_a_system_event_is_nobodys_note_and_leaves_the_denominator() -> None:
    """ASSUMPTION[Q6]: a backend-generated timeline entry is not a
    salesperson's work, and counting it would dilute their flagged share."""
    rows = _rows(3, flagged=True) + [
        _row(note_id=index, total=None, note_type="system_event") for index in (4, 5, 6)
    ]
    measure = flagged_share(rows, SMALL_FLOOR)
    assert (measure.counted, measure.of, measure.percent) == (3, 3, 100)


def test_a_rep_with_no_notes_has_no_flagged_share() -> None:
    measure = flagged_share([], SMALL_FLOOR)
    assert measure.percent is None
    assert measure.suppressed is MeasureSuppressed.NOTHING_TO_MEASURE


def test_a_flagged_share_below_the_floor_carries_no_percent() -> None:
    measure = flagged_share(_rows(2, flagged=True), SMALL_FLOOR)
    assert measure.percent is None
    assert measure.suppressed is MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    # The counts are still carried: a brief can say how far off the floor it is.
    assert (measure.counted, measure.of) == (2, 2)


# --- the improved share -----------------------------------------------------


def _asked(note_id: int, total: int, minutes: int = 0) -> JudgementRow:
    return _row(
        note_id=note_id, total=total, flagged=True, prompt_sent=True, minutes=minutes
    )


def test_a_resubmission_is_a_later_row_for_the_same_note_at_fair_or_better() -> None:
    """The link the row does not carry, reconstructed from what it does: the
    note id plus recorded order. Register item 144: the bar is fair or better,
    so a note asked about at fair that comes back fair has met it."""
    rows = [
        _asked(1, 50),
        _row(note_id=1, total=90),  # the resubmission: fair -> excellent
        _asked(2, 50),
        _row(note_id=2, total=55),  # still fair: at the bar, so improved
        _asked(3, 50),  # never came back
    ]
    measure = improved_share(rows, SMALL_FLOOR)
    assert measure.name is MeasureName.IMPROVED_SHARE
    assert (measure.counted, measure.of, measure.percent) == (2, 3, 67)


def test_a_better_total_that_stays_poor_is_not_an_improvement() -> None:
    """The measure is in bands, and the bar is fair: a rise from 10 to 35 is
    still a poor note (register item 144)."""
    rows = [_asked(1, 10), _row(note_id=1, total=35), _asked(2, 41), _asked(3, 41)]
    assert improved_share(rows, SMALL_FLOOR).counted == 0


def test_a_suppressed_note_that_came_back_poor_is_not_improved() -> None:
    """Scored at last is not enough: the note must reach fair."""
    rows = [
        _row(note_id=1, total=None, flagged=True, prompt_sent=True, note_type=None),
        _row(note_id=1, total=20),
        _asked(2, 50),
        _asked(3, 50),
    ]
    assert improved_share(rows, SMALL_FLOOR).counted == 0


def test_an_earlier_better_row_is_not_a_resubmission() -> None:
    """Order is the whole mechanism: a note that was good and got worse must
    not read as an improvement because a better band exists somewhere."""
    rows = [_row(note_id=1, total=90), _asked(1, 50), _asked(2, 50), _asked(3, 50)]
    assert improved_share(rows, SMALL_FLOOR).counted == 0


def test_a_suppressed_note_that_came_back_scored_counts_as_improved() -> None:
    """It went from unscorable to fair, which is what being asked was for:
    the bar is where the note ended up (register item 144)."""
    rows = [
        _row(note_id=1, total=None, flagged=True, prompt_sent=True, note_type=None),
        _row(note_id=1, total=45),
        _asked(2, 50),
        _asked(3, 50),
    ]
    assert improved_share(rows, SMALL_FLOOR).counted == 1


def test_only_notes_we_actually_asked_about_are_in_the_denominator() -> None:
    """A prompt that was withheld never reached the salesperson. Counting it
    would report them as ignoring a question they were never asked."""
    rows = _rows(5, flagged=True, prompt_sent=False) + [_asked(6, 50), _asked(7, 50)]
    measure = improved_share(
        rows, dataclasses.replace(CONFIG, measure_evidence_floor=2)
    )
    assert measure.of == 2


def test_a_rep_nobody_asked_anything_has_no_improved_share() -> None:
    """Often good news, and told as such: a zero denominator is its own reason
    and never a 0%."""
    measure = improved_share(_rows(20), CONFIG)
    assert measure.percent is None
    assert measure.suppressed is MeasureSuppressed.NOTHING_TO_MEASURE


def test_two_asks_on_one_note_are_judged_on_what_followed_each() -> None:
    """A tenant whose clarification_cap is above 1 asks twice; the second ask
    must not be credited with the improvement that followed the first."""
    rows = [
        _asked(1, 30),
        _row(note_id=1, total=50),  # poor -> fair, after the first ask
        _asked(1, 50),  # asked again, and nothing followed
        _asked(2, 50),
    ]
    measure = improved_share(rows, SMALL_FLOOR)
    assert (measure.counted, measure.of) == (1, 3)


# --- the three together -----------------------------------------------------


def test_measure_rep_carries_the_window_it_was_computed_over() -> None:
    """A correct figure described with the wrong period is a wrong statement,
    and that is the commonest way one is made."""
    since, until = rolling_window(datetime(2026, 9, 30, tzinfo=UTC), CONFIG)
    measures = measure_rep(
        _rows(12, flagged=True),
        author_id=AUTHOR,
        since=since,
        until=until,
        config=CONFIG,
    )
    assert (measures.author_id, measures.since, measures.until) == (
        AUTHOR,
        since,
        until,
    )


def test_the_floor_applies_per_measure_not_per_rep() -> None:
    """A rep with plenty of notes and two prompts has an average and a flagged
    share, and no improvement figure."""
    rows = _rows(12, flagged=True) + [_asked(90, 50), _asked(91, 50)]
    measures = measure_rep(
        rows,
        author_id=AUTHOR,
        since=START,
        until=START + timedelta(days=30),
        config=CONFIG,
    )
    assert measures.average_band.suppressed is None
    assert measures.flagged_share.suppressed is None
    assert measures.improved_share.suppressed is MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    assert measures.improved_share.percent is None


def test_a_suppressed_measure_never_carries_a_figure() -> None:
    """The shape that would be rendered: a reason beside a number is the bug
    this module exists to make impossible."""
    measures = measure_rep(
        _rows(2),
        author_id=AUTHOR,
        since=START,
        until=START + timedelta(days=30),
        config=CONFIG,
    )
    assert measures.average_band.band is None
    assert measures.flagged_share.percent is None
    assert measures.improved_share.percent is None


def test_the_measures_read_nothing_but_rows_and_config() -> None:
    """Pure: the same rows give the same answer, with no clock and no store."""
    rows = _rows(12, flagged=True)
    first = measure_rep(
        rows,
        author_id=AUTHOR,
        since=START,
        until=START + timedelta(days=30),
        config=CONFIG,
    )
    second = measure_rep(
        rows,
        author_id=AUTHOR,
        since=START,
        until=START + timedelta(days=30),
        config=CONFIG,
    )
    assert first == second


def _config_with(**changes: object) -> TenantConfig:
    return dataclasses.replace(CONFIG, **changes)


def test_a_tenant_may_raise_its_own_floor() -> None:
    """The floor is a tenant's call: a team that wants a month of evidence
    before a manager sees a band can have one."""
    strict = _config_with(measure_evidence_floor=20)
    assert average_band(_rows(12), strict).suppressed is (
        MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    )
    assert average_band(_rows(20), strict).suppressed is None


# --- whole local days (register item 156) ----------------------------------


def test_a_brief_at_0730_and_one_at_1100_local_share_a_window() -> None:
    """Today is never in the window, so the hour of the call cannot move it."""
    early = datetime(2026, 9, 30, 3, 30, tzinfo=UTC)  # 07:30 in Dubai
    late = datetime(2026, 9, 30, 7, 0, tzinfo=UTC)  # 11:00 in Dubai
    assert rolling_window(early, CONFIG) == rolling_window(late, CONFIG)


def test_a_daylight_saving_zone_keeps_local_midnight_bounds() -> None:
    """Across London's spring change the bounds are still local midnights, so
    the week is an hour short in UTC and whole in local days."""
    london = dataclasses.replace(
        CONFIG, timezone="Europe/London", rolling_window_days=7
    )
    since, until = rolling_window(datetime(2026, 3, 30, 12, tzinfo=UTC), london)
    zone = ZoneInfo("Europe/London")
    assert since == datetime(2026, 3, 23, tzinfo=zone)
    assert until == datetime(2026, 3, 30, tzinfo=zone)
    assert (since.astimezone(zone).hour, until.astimezone(zone).hour) == (0, 0)
    assert until - since == timedelta(days=7) - timedelta(hours=1)


def test_the_printed_period_is_local_dates_first_to_last() -> None:
    """A Dubai window named by its local first and last days."""
    since, until = rolling_window(datetime(2026, 9, 21, 6, tzinfo=UTC), CONFIG)
    assert local_dates(since, until, CONFIG) == (date(2026, 8, 22), date(2026, 9, 20))


def test_the_zone_is_parsed_and_an_unknown_one_refused() -> None:
    """An IANA name is accepted; anything else fails with no value quoted."""
    assert CONFIG.timezone == "Asia/Dubai"
    assert CONFIG.config_version == "tenant-cfg-default-4"
    london = parse_unit_a_section({"config_version": "v", "timezone": "Europe/London"})
    assert london.timezone == "Europe/London"
    unset = parse_unit_a_section({"config_version": "v", "timezone": None})
    assert unset.timezone == "Asia/Dubai"
    for bad in ("Mars/Olympus", "../../etc/passwd", ""):
        with pytest.raises(ValueError) as caught:
            parse_unit_a_section({"config_version": "v", "timezone": bad})
        assert "Olympus" not in str(caught.value.errors(include_input=False))  # type: ignore[attr-defined]
