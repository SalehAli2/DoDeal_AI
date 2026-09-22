"""The three per-rep measures, computed in code from stored judgement rows.

WHAT THEY ARE (register item 144), over a rolling window of a tenant's rows:

  average band      the rep's typical note, AS A BAND. Not a number: a mean of
                    72.4 invites a manager to argue about a decimal the rubric
                    cannot justify, and the band is the resolution the whole
                    unit was calibrated at.
  flagged share     what proportion of their notes we had something to say
                    about.
  improved share    of the notes we actually ASKED about, what proportion came
                    back at fair or better (register item 144).

NO MODEL TOUCHES ANY OF IT. Every figure here is arithmetic over rows, in
integers, exactly as compute_score is -- the same reason: a measure a manager
acts on must be reproducible from the rows, and two runs over the same rows
must not differ.

THE EVIDENCE FLOOR IS THE POINT OF THE MODULE. Below `measure_evidence_floor`
rows a measure is SUPPRESSED with a reason and is NEVER reported as a low
number. A rep with three notes has no average; showing one anyway is a manager
acting on noise, and the person it is about has no way to tell the difference.
The floor is per tenant (config.py) and each measure applies it to ITS OWN
denominator, because the three have different denominators and a rep may have
plenty of notes and nothing we ever asked about.

A DENOMINATOR OF ZERO IS ITS OWN REASON. "We never asked you anything" is good
news; "there is not enough to say yet" is a wait. Reporting both as one code
would make the brief word them the same way.

SYSTEM EVENTS ARE NOT A SALESPERSON'S WORK and leave every denominator. They
are backend-generated timeline entries (ASSUMPTION[Q6]); counting them would
dilute a rep's flagged share with rows nobody wrote.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.judgement_rows import JudgementRow
from dodeal_ai.units.structured_intelligence.schemas import (
    Band,
    EnforcementVerdict,
    NoteType,
)


class MeasureName(StrEnum):
    """The three, named once. A StrEnum rather than three bare strings for the
    same reason every other vocabulary here is one: a template that names a
    measure and a measure that names itself must not be able to disagree."""

    AVERAGE_BAND = "average_band"
    FLAGGED_SHARE = "flagged_share"
    IMPROVED_SHARE = "improved_share"


class MeasureSuppressed(StrEnum):
    """Why there is no figure. A suppressed measure is a STATE with a reason,
    never a zero and never a low percentage -- the same rule a suppressed
    judgement is held to.

    nothing_to_measure  the denominator is zero. Nothing was written, or
                        nothing was ever asked about. Often good news.
    below_evidence_floor there is some evidence and not enough of it. A wait,
                        not a result.
    """

    NOTHING_TO_MEASURE = "nothing_to_measure"
    BELOW_EVIDENCE_FLOOR = "below_evidence_floor"


@dataclass(frozen=True, slots=True)
class BandMeasure:
    """The rolling average band, or the reason there is not one.

    `band` and `suppressed` are exclusive: exactly one is set. `notes` is what
    it was computed over and is reported either way -- a brief that says "not
    enough yet" is more use when it can say how far off it is.

    `excluded` counts scored rows dropped as NOT COMPARABLE: see
    `average_band()`. It is carried rather than swallowed, because a measure
    quietly computed over half a rep's notes is worse than one that says so.
    """

    band: Band | None
    suppressed: MeasureSuppressed | None
    notes: int
    excluded: int


@dataclass(frozen=True, slots=True)
class ShareMeasure:
    """One of the two proportions, or the reason there is not one.

    `percent` is a whole number, rounded half up in integers -- no float ever
    reaches a figure a manager is shown, for the same reason none reaches a
    total. `counted` and `of` are kept so the brief can say "3 of 14" rather
    than only "21%", which is what a rep asks next.
    """

    name: MeasureName
    percent: int | None
    suppressed: MeasureSuppressed | None
    counted: int
    of: int


@dataclass(frozen=True, slots=True)
class RepMeasures:
    """One salesperson's three measures over one window.

    The window is carried so a brief cannot describe a figure with a period it
    was not computed over -- the commonest way a correct number becomes a
    wrong statement.
    """

    author_id: int
    since: datetime
    until: datetime
    average_band: BandMeasure
    flagged_share: ShareMeasure
    improved_share: ShareMeasure


def local_midnight(day: date, config: TenantConfig) -> datetime:
    """The instant `day` begins in the tenant's zone, in UTC (register item
    156). Built from the local date, so a daylight-saving change moves the
    UTC instant and never the local midnight."""
    zone = ZoneInfo(config.timezone)
    return datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)


def local_today(now: datetime, config: TenantConfig) -> date:
    """Today's date in the tenant's zone."""
    return now.astimezone(ZoneInfo(config.timezone)).date()


def rolling_window(now: datetime, config: TenantConfig) -> tuple[datetime, datetime]:
    """The half-open window the store read takes: WHOLE LOCAL DAYS (register
    item 156), `[local midnight today - rolling_window_days, local midnight
    today)`, as UTC instants. Today is never in it, so a brief at 07:30 and one
    at 11:00 describe the same period.

    Here rather than at each call site so the 30 lives in exactly one place --
    the tenant's config -- and two callers cannot measure different periods and
    describe them with the same sentence.
    """
    today = local_today(now, config)
    start = today - timedelta(days=config.rolling_window_days)
    return local_midnight(start, config), local_midnight(today, config)


def local_dates(
    since: datetime, until: datetime, config: TenantConfig
) -> tuple[date, date]:
    """The first and last LOCAL dates inside `[since, until)`: what a printed
    period names. `until` is exclusive, so the last date is the one just before
    it."""
    zone = ZoneInfo(config.timezone)
    last = until - timedelta(microseconds=1)
    return since.astimezone(zone).date(), last.astimezone(zone).date()


def _rep_notes(rows: Iterable[JudgementRow]) -> list[JudgementRow]:
    """The rows that are a salesperson's own work.

    A `system_event` row is a backend-generated timeline entry (ASSUMPTION[Q6]),
    never scored and never anybody's to answer for. It leaves every denominator
    here; keeping it would dilute a rep's flagged share with rows nobody wrote.
    """
    return [row for row in rows if row.note_type != NoteType.SYSTEM_EVENT]


def _suppression(denominator: int, config: TenantConfig) -> MeasureSuppressed | None:
    """The reason this denominator carries no figure, or None when it does."""
    if denominator == 0:
        return MeasureSuppressed.NOTHING_TO_MEASURE
    if denominator < config.measure_evidence_floor:
        return MeasureSuppressed.BELOW_EVIDENCE_FLOOR
    return None


def _percent(counted: int, of: int) -> int:
    """`counted` out of `of` as a whole percent, round half up, in integers.

    The same idiom as compute_score's rescaling, deliberately: one rounding
    rule in the unit means a figure in a brief and a total on a judgement
    cannot disagree about what 0.5 does.
    """
    return (counted * 100 + of // 2) // of


def average_band(rows: Sequence[JudgementRow], config: TenantConfig) -> BandMeasure:
    """The rep's typical note over the window, as a band.

    THE TOTALS ARE AVERAGED AND THE BAND IS DERIVED, through the same
    `config.band_for` every individual band comes from. Averaging band
    positions instead would be a second way to produce a band, and the two
    would disagree the first time a boundary moved.

    ONLY COMPARABLE ROWS ARE AVERAGED. A total is a percentage of a
    denominator the rubric and the tenant's config chose, so totals made under
    a different `rubric_version` or `config_version` are not the same
    measurement. The most recent scored row's pair wins and the rest are
    EXCLUDED AND COUNTED -- a drift a reader can see beats a silently mixed
    average. `prompt_version` and `model_version` are deliberately NOT part of
    the pair: they change what the model answered, which is the noise the
    average exists to absorb, and fragmenting on them would suppress every
    measure the day a provider rolls a point release.

    Suppressed rows carry no band and cannot be averaged into anything; they
    are not in the denominator and are not a zero.
    """
    scored = [row for row in _rep_notes(rows) if row.total is not None]
    if not scored:
        return BandMeasure(
            band=None,
            suppressed=MeasureSuppressed.NOTHING_TO_MEASURE,
            notes=0,
            excluded=0,
        )

    latest = scored[-1]
    stamp = (latest.rubric_version, latest.config_version)
    comparable = [
        row for row in scored if (row.rubric_version, row.config_version) == stamp
    ]
    excluded = len(scored) - len(comparable)

    suppressed = _suppression(len(comparable), config)
    if suppressed is not None:
        return BandMeasure(
            band=None,
            suppressed=suppressed,
            notes=len(comparable),
            excluded=excluded,
        )

    totals = [row.total for row in comparable if row.total is not None]
    mean = (sum(totals) * 2 + len(totals)) // (2 * len(totals))
    return BandMeasure(
        band=config.band_for(mean),
        suppressed=None,
        notes=len(comparable),
        excluded=excluded,
    )


def flagged_share(rows: Sequence[JudgementRow], config: TenantConfig) -> ShareMeasure:
    """What proportion of the rep's notes we had something to say about.

    The denominator is every note they wrote in the window, scored or
    suppressed: a note below the length floor is flagged and is theirs to fix,
    so leaving suppressed rows out would hide the very notes the measure is
    most about.

    The numerator is the ENFORCEMENT VERDICT, not the decision action: the
    verdict is what the CRM acted on, and it already accounts for a tenant
    running `off`. A rep at a tenant with enforcement off has a flagged share
    of zero, which is true -- nothing was flagged to anyone.
    """
    notes = _rep_notes(rows)
    flagged = sum(
        1 for row in notes if row.enforcement_verdict is EnforcementVerdict.FLAG
    )
    suppressed = _suppression(len(notes), config)
    return ShareMeasure(
        name=MeasureName.FLAGGED_SHARE,
        percent=None if suppressed else _percent(flagged, len(notes)),
        suppressed=suppressed,
        counted=flagged,
        of=len(notes),
    )


def improved_share(rows: Sequence[JudgementRow], config: TenantConfig) -> ShareMeasure:
    """Of the notes we actually ASKED about, what proportion came back better.

    THE BUSINESS CRITERION (register item 144): a note we asked about counts
    as improved when a LATER row for the same note id is band fair or better.
    No row links a resubmission to the judgement it followed, and no row
    records an edit, so "a later row for the same note" is the link, and the
    bar is the business's own -- fair or better -- rather than "better than
    before". A note asked about at fair that comes back fair has met the bar;
    one that rises from poor and stays poor has not.

    The denominator is notes where a prompt was actually SENT, not every
    flagged note. A prompt that was withheld -- capped, rate limited, or a
    resubmission -- never reached the salesperson, and counting it would report
    them as ignoring a question they were never asked.

    "Later" is position in the sequence, which is why the store promises
    recorded order: both rows for a resubmitted note carry the same
    `note_created_at`, so the timestamp cannot order them.
    """
    notes = _rep_notes(rows)
    asked = [row for row in notes if row.prompt_sent]
    # Each asked row against the bands the SAME note reached after IT, not
    # after the first ask: a tenant whose clarification_cap is above 1 asks
    # twice, and the second ask must be judged on what followed the second.
    improved = sum(
        1
        for position, row in enumerate(notes)
        if row.prompt_sent
        and _improved(
            [
                later.band
                for later in notes[position + 1 :]
                if later.note_id == row.note_id and later.band is not None
            ],
        )
    )
    suppressed = _suppression(len(asked), config)
    return ShareMeasure(
        name=MeasureName.IMPROVED_SHARE,
        percent=None if suppressed else _percent(improved, len(asked)),
        suppressed=suppressed,
        counted=improved,
        of=len(asked),
    )


# The business's bar for "improved" (register item 144): a later band at this
# or above. Named once, so the criterion is read from here and not re-derived.
_IMPROVED_AT = Band.FAIR


def _improved(later: list[Band]) -> bool:
    """Did any row after the asked-about one reach fair or better?

    The asked row's own band does not enter into it: the criterion is where
    the note ended up, so a suppressed note that came back fair counts and one
    that came back poor does not.
    """
    order = list(Band)
    return any(order.index(band) >= order.index(_IMPROVED_AT) for band in later)


def measure_rep(
    rows: Sequence[JudgementRow],
    *,
    author_id: int,
    since: datetime,
    until: datetime,
    config: TenantConfig,
) -> RepMeasures:
    """The three measures for one salesperson. Pure: no I/O, no clock, no model.

    `rows` are already this author's and already inside `[since, until)` -- the
    store's read does both, and doing it again here would be a second filter to
    keep in step with the first. The window is carried onto the result so the
    brief describes the period the figures were actually computed over.
    """
    return RepMeasures(
        author_id=author_id,
        since=since,
        until=until,
        average_band=average_band(rows, config),
        flagged_share=flagged_share(rows, config),
        improved_share=improved_share(rows, config),
    )
