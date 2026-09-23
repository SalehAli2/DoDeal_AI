"""The three role briefs — text, from arithmetic, with no model call.

THREE VERSIONS, because there are three people who read one (register item
145): the rep's own work, the team leader's team, the head of sales across
teams. They are the same three measures rendered at three altitudes, not three
different sets of figures, so a rep and their manager can never be looking at
numbers that disagree.

WHAT THE BUSINESS ASKED FOR (register item 145), all from judgement rows and
all under the evidence floor: the rep's yesterday (band, flag count, signals);
the team leader's week against last week, who was flagged yesterday and the
team's signals; the head of sales's teams week on week and a coaching list.
"Yesterday" is the last whole local day, a week the last seven. Ids travel in
the JSON, never in the text.

NO MODEL TOUCHES A BRIEF. Every number is arithmetic from measures.py and every
sentence is a template in this file. A brief is read as fact -- it is the thing
a manager acts on before they have spoken to anyone -- and a model asked to
narrate figures will eventually produce one that is not in them. There is
nothing to gain here that is worth that: the sentences are short and there are
six of them.

A BRIEF IS DATA AS WELL AS TEXT (register item 154): `build_brief` returns a
`Brief` -- the text, the period in local dates, every line's measures and the
flagged note ids -- and the route answers it as JSON.

A BRIEF WITH NOTHING TO REPORT IS NOT SENT. `build_brief` returns None, and the
route answers 204. An empty daily email trains people to ignore the channel
within a fortnight, and once they do, the one that matters is ignored with the
rest. "Nothing to report" means no line carries a single figure -- a brief that
would say only "not enough evidence yet, three times" is an empty email with
extra words.

NO GREETING AND NO TIME OF DAY. The CRM calls at 07:30 today, but this is an
on-demand route with no scheduler of its own, and a brief that says "good
morning" at four in the afternoon is a small wrongness in a document whose
whole value is that it is not wrong.

A SUPPRESSED MEASURE IS STATED, NEVER FILLED IN. It renders as "not enough
yet", with the count and the floor, so a rep can see how far off it is -- never
as a low band and never as 0%.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta

from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.judgement_rows import JudgementRow
from dodeal_ai.units.structured_intelligence.measures import (
    BandMeasure,
    MeasureSuppressed,
    ShareMeasure,
    average_band,
    flagged_share,
    improved_share,
    local_dates,
    local_midnight,
    local_today,
    rolling_window,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Band,
    EnforcementVerdict,
    NoteType,
)
from dodeal_ai.units.structured_intelligence.user_directory import (
    Role,
    User,
    team_key,
)

# The most note ids any one list in a brief carries, newest first. A list
# longer than this is not read; the CRM links to the rest.
MAX_NOTE_IDS = 50

# The note types a manager wants to hear about the next morning: the ones that
# move a deal (register item 145). Their notes are listed by id and type.
SIGNAL_TYPES: frozenset[str] = frozenset(
    {NoteType.VIEWING, NoteType.NEGOTIATION, NoteType.WON_LOST}
)

# A person is on the head of sales's coaching list when their typical note was
# one of these in EACH of the last two weeks (register item 145).
COACHING_BANDS: frozenset[Band] = frozenset({Band.POOR, Band.FAIR})

# A "week" is this many whole local days, compared with the same before it.
WEEK_DAYS = 7

# How wide a name column is before the figures start. Fixed rather than
# computed from the longest name, so two briefs a week apart line up in a
# terminal and in an email; a longer name simply pushes its own row out.
_NAME_WIDTH = 22


@dataclass(frozen=True, slots=True)
class BriefLine:
    """One named subject and its three measures over one period, ready to render.

    The team's own roll-up and each person in it are the SAME shape, which is
    what keeps a team total and the rows under it from being computed two
    different ways. `period` says which stretch of days (register item 145):
    "rolling", "yesterday", "this_week" or "last_week".
    """

    name: str
    average_band: BandMeasure
    flagged_share: ShareMeasure
    improved_share: ShareMeasure
    period: str = "rolling"

    @property
    def has_a_figure(self) -> bool:
        """Is there anything here to tell somebody? A line where all three are
        suppressed says nothing, and a brief of only those is not sent."""
        return (
            self.average_band.suppressed is None
            or self.flagged_share.suppressed is None
            or self.improved_share.suppressed is None
        )


@dataclass(frozen=True, slots=True)
class Signal:
    """A note of a type worth hearing about the next morning: its id and type."""

    note_id: int
    note_type: str


@dataclass(frozen=True, slots=True)
class FlaggedPerson:
    """Somebody with flagged notes yesterday, and which notes, newest first."""

    name: str
    note_ids: list[int]


@dataclass(frozen=True, slots=True)
class Brief:
    """One brief as the route answers it: the text a person reads, and the
    same content as data the CRM can render or link from. Ids travel here and
    never in the text (see test_no_brief_ever_prints_an_id)."""

    text: str
    first_day: date
    last_day: date
    days: int
    lines: list[BriefLine]
    flagged_note_ids: list[int]
    signals: list[Signal] = field(default_factory=list)
    flagged_yesterday: list[FlaggedPerson] = field(default_factory=list)
    coaching: list[str] = field(default_factory=list)

    def body(self) -> dict[str, object]:
        """The JSON body. Ids and figures only: no row carries note text."""
        return {
            "text": self.text,
            "period": {
                "first_day": self.first_day.isoformat(),
                "last_day": self.last_day.isoformat(),
                "days": self.days,
            },
            "lines": [asdict(line) for line in self.lines],
            "flagged_note_ids": self.flagged_note_ids,
            "signals": [asdict(signal) for signal in self.signals],
            "flagged_yesterday": [asdict(person) for person in self.flagged_yesterday],
            "coaching": [{"name": name} for name in self.coaching],
        }


@dataclass(frozen=True, slots=True)
class _Days:
    """The whole local days the business's sections are about (item 145)."""

    yesterday: tuple[datetime, datetime]
    this_week: tuple[datetime, datetime]
    last_week: tuple[datetime, datetime]


@dataclass(slots=True)
class _Parts:
    """What one role's builder produced, before the not-sent rule is applied."""

    text: list[str]
    lines: list[BriefLine]
    covered: list[JudgementRow]
    signals: list[Signal] = field(default_factory=list)
    flagged_yesterday: list[FlaggedPerson] = field(default_factory=list)
    coaching: list[str] = field(default_factory=list)


def newest_note_ids(rows: Sequence[JudgementRow]) -> list[int]:
    """Distinct note ids, newest note first (a later-recorded row first on a
    tie), at most MAX_NOTE_IDS."""
    ordered = sorted(
        enumerate(rows),
        key=lambda pair: (pair[1].note_created_at, pair[0]),
        reverse=True,
    )
    ids: list[int] = []
    for _, row in ordered:
        if row.note_id not in ids:
            ids.append(row.note_id)
        if len(ids) == MAX_NOTE_IDS:
            break
    return ids


def _flagged(rows: Sequence[JudgementRow]) -> list[int]:
    return newest_note_ids(
        [row for row in rows if row.enforcement_verdict is EnforcementVerdict.FLAG]
    )


def _signals(rows: Sequence[JudgementRow]) -> list[Signal]:
    """The signal notes among `rows`, newest first, at most MAX_NOTE_IDS."""
    type_of = {row.note_id: row.note_type for row in rows}
    return [
        Signal(note_id=note_id, note_type=str(type_of[note_id]))
        for note_id in newest_note_ids(
            [row for row in rows if row.note_type in SIGNAL_TYPES]
        )
    ]


def _within(
    rows: Sequence[JudgementRow], bounds: tuple[datetime, datetime]
) -> list[JudgementRow]:
    since, until = bounds
    return [row for row in rows if since <= row.note_created_at < until]


def _days(since: datetime, until: datetime, config: TenantConfig) -> _Days:
    """Yesterday and the two weeks, ending where the rolling window ends."""
    today = local_dates(since, until, config)[1] + timedelta(days=1)

    def midnight(days_back: int) -> datetime:
        return local_midnight(today - timedelta(days=days_back), config)

    return _Days(
        yesterday=(midnight(1), midnight(0)),
        this_week=(midnight(WEEK_DAYS), midnight(0)),
        last_week=(midnight(2 * WEEK_DAYS), midnight(WEEK_DAYS)),
    )


def brief_window(now: datetime, config: TenantConfig) -> tuple[datetime, datetime]:
    """The store read a brief takes: the rolling window, widened when needed
    to cover the two weeks the sections compare (register item 145)."""
    since, until = rolling_window(now, config)
    earliest = local_midnight(
        local_today(now, config) - timedelta(days=2 * WEEK_DAYS), config
    )
    return min(since, earliest), until


def _line(
    name: str,
    rows: Sequence[JudgementRow],
    config: TenantConfig,
    period: str = "rolling",
) -> BriefLine:
    """The three measures over one set of rows, under one name."""
    return BriefLine(
        name=name,
        average_band=average_band(rows, config),
        flagged_share=flagged_share(rows, config),
        improved_share=improved_share(rows, config),
        period=period,
    )


def _not_enough(count: int, config: TenantConfig) -> str:
    """How a suppressed measure reads. The count AND the floor, so the sentence
    is a wait with a distance rather than a refusal."""
    return f"not enough yet ({count} of {config.measure_evidence_floor})"


def _band_phrase(measure: BandMeasure, config: TenantConfig) -> str:
    if measure.suppressed is MeasureSuppressed.NOTHING_TO_MEASURE:
        return "no scored notes"
    if measure.suppressed is not None or measure.band is None:
        return _not_enough(measure.notes, config)
    return f"{measure.band.value} over {measure.notes} notes"


def _share_phrase(measure: ShareMeasure, config: TenantConfig, *, empty: str) -> str:
    if measure.suppressed is MeasureSuppressed.NOTHING_TO_MEASURE:
        return empty
    if measure.suppressed is not None:
        return _not_enough(measure.of, config)
    return f"{measure.counted} of {measure.of} ({measure.percent}%)"


def _flagged_phrase(measure: ShareMeasure, config: TenantConfig) -> str:
    return _share_phrase(measure, config, empty="no notes")


def _improved_phrase(measure: ShareMeasure, config: TenantConfig) -> str:
    # "Nothing was asked about" rather than 0%: it is usually good news, and a
    # zero would read as a rep who ignored every question.
    return _share_phrase(measure, config, empty="nothing was asked about")


def _block(line: BriefLine, config: TenantConfig) -> list[str]:
    """One subject in full, three labelled rows. Used for whoever the brief is
    ABOUT -- the rep, the team, all teams."""
    return [
        f"  Typical note           {_band_phrase(line.average_band, config)}",
        f"  Flagged                {_flagged_phrase(line.flagged_share, config)}",
        f"  Improved after asking  {_improved_phrase(line.improved_share, config)}",
    ]


def _row_line(line: BriefLine, config: TenantConfig, label: str | None = None) -> str:
    """One subject on one line, for the lists under a team or an org brief."""
    return (
        f"  {label or line.name:<{_NAME_WIDTH}}"
        f"typical {_band_phrase(line.average_band, config)}"
        f" · flagged {_flagged_phrase(line.flagged_share, config)}"
        f" · improved {_improved_phrase(line.improved_share, config)}"
    )


def _period(since: datetime, until: datetime, config: TenantConfig) -> str:
    """The window, in the tenant's LOCAL dates (register item 156), first day
    to last day inclusive. Stated rather than assumed, because a correct figure
    described with the wrong period is a wrong statement.
    """
    first, last = local_dates(since, until, config)
    return f"{first:%Y-%m-%d} to {last:%Y-%m-%d} ({config.rolling_window_days} days)"


def _by_author(rows: Sequence[JudgementRow], author_id: int) -> list[JudgementRow]:
    return [row for row in rows if row.author_id == author_id]


def _signal_sentence(signals: list[Signal]) -> list[str]:
    """How many signal notes of each type yesterday -- counts, never ids."""
    counts = Counter(signal.note_type for signal in signals)
    listed = ", ".join(f"{counts[name]} {name}" for name in sorted(counts))
    return ["", f"Signals yesterday: {listed}."]


def _weeks(
    name: str, rows: Sequence[JudgementRow], days: _Days, config: TenantConfig
) -> tuple[BriefLine, BriefLine]:
    return (
        _line(name, _within(rows, days.this_week), config, period="this_week"),
        _line(name, _within(rows, days.last_week), config, period="last_week"),
    )


def _rep_brief(
    subject: User,
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
    days: _Days,
) -> _Parts:
    """The rep's own work: the rolling measures, yesterday's band and flag count,
    and yesterday's signals. `rows` are already theirs -- the route narrows the
    store read by author, so nothing is filtered twice."""
    rolling = _within(rows, (since, until))
    line = _line(subject.name, rolling, config)
    text = [
        f"Note quality for {subject.name}.",
        _period(since, until, config),
        "",
        *_block(line, config),
    ]
    lines = [line]
    yesterday_rows = _within(rows, days.yesterday)
    yesterday = _line(subject.name, yesterday_rows, config, period="yesterday")
    if yesterday.has_a_figure:
        # The band and the flag count; improvement is not a one-day figure.
        text += ["", "Yesterday", *_block(yesterday, config)[:2]]
        lines.append(yesterday)
    signals = _signals(yesterday_rows)
    if signals:
        text += _signal_sentence(signals)
    return _Parts(text, lines, rolling, signals=signals)


def _team_brief(
    subject: User,
    users: Sequence[User],
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
    days: _Days,
) -> _Parts:
    """The team leader's team: the team as a whole, then each person in it,
    then this week against last week, who was flagged yesterday, and the
    team's signals.

    The team's roll-up is computed over the team's rows POOLED, not averaged
    from the per-person figures. Averaging averages would weight a rep with two
    notes the same as one with forty, which is the arithmetic mistake a team
    figure is most often reported with.
    """
    if subject.team is None:
        # A team brief for somebody who is in no team. There is no team to
        # report on, and inventing one would be worse than saying nothing: no
        # lines means no figures, which build_brief turns into a 204.
        return _Parts([], [], [])

    # The leader is in their own team and appears in the list. They write notes
    # too, and a team figure their own work is inside should say so.
    key = team_key(subject.team)
    members = sorted(
        (
            user
            for user in users
            if user.team is not None and team_key(user.team) == key
        ),
        key=lambda user: user.name,
    )
    member_ids = {user.user_id for user in members}
    all_team_rows = [row for row in rows if row.author_id in member_ids]
    team_rows = _within(all_team_rows, (since, until))

    team = _line(f"Team {subject.team}", team_rows, config)
    people = [
        _line(user.name, _by_author(team_rows, user.user_id), config)
        for user in members
    ]

    text = [
        f"Note quality for {subject.name}'s team ({subject.team}).",
        _period(since, until, config),
        "",
        "The team",
        *_block(team, config),
    ]
    if people:
        text += ["", "Each person", *(_row_line(line, config) for line in people)]
    lines = [team, *people]

    this_week, last_week = _weeks(f"Team {subject.team}", all_team_rows, days, config)
    if this_week.has_a_figure or last_week.has_a_figure:
        text += [
            "",
            "This week against last week",
            _row_line(this_week, config, label="This week"),
            _row_line(last_week, config, label="Last week"),
        ]
        lines += [this_week, last_week]

    yesterday_rows = _within(all_team_rows, days.yesterday)
    flagged_people = [
        FlaggedPerson(name=user.name, note_ids=ids)
        for user in members
        if (ids := _flagged(_by_author(yesterday_rows, user.user_id)))
    ]
    if flagged_people:
        text += [
            "",
            "Flagged yesterday",
            *(
                f"  {person.name:<{_NAME_WIDTH}}{len(person.note_ids)} notes"
                for person in flagged_people
            ),
        ]
    signals = _signals(yesterday_rows)
    if signals:
        text += _signal_sentence(signals)
    return _Parts(
        text,
        lines,
        team_rows,
        signals=signals,
        flagged_yesterday=flagged_people,
    )


def _coached(
    user: User, rows: Sequence[JudgementRow], days: _Days, config: TenantConfig
) -> bool:
    """Poor or fair in EACH of the last two weeks, each week over the floor."""
    theirs = _by_author(rows, user.user_id)
    return all(
        average_band(_within(theirs, week), config).band in COACHING_BANDS
        for week in (days.this_week, days.last_week)
    )


def _org_brief(
    subject: User,
    users: Sequence[User],
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
    days: _Days,
) -> _Parts:
    """The head of sales, across teams: every team this week against last week,
    and the people whose notes need coaching. Teams, not people, otherwise:
    naming every rep in the company is a list nobody reads."""
    # Grouped by team_key; each team is shown in its first spelling in sorted
    # order, so the label never depends on the order the directory lists people.
    team_of = {
        user.user_id: team_key(user.team) for user in users if user.team is not None
    }
    shown: dict[str, str] = {}
    for spelling in sorted(user.team for user in users if user.team is not None):
        shown.setdefault(team_key(spelling), spelling)
    teams = sorted(shown)

    attributed = [row for row in rows if row.author_id in team_of]
    everything = _within(attributed, (since, until))
    # Missing from the directory, not merely teamless: a head of sales is listed
    # and in no team, and their own notes are no sign the directory is stale.
    listed = {user.user_id for user in users}
    unlisted = sum(
        1 for row in _within(rows, (since, until)) if row.author_id not in listed
    )

    total = _line("All teams", everything, config)
    by_team = [
        _line(
            shown[team],
            [row for row in everything if team_of[row.author_id] == team],
            config,
        )
        for team in teams
    ]

    text = [
        f"Note quality across all teams, for {subject.name}.",
        _period(since, until, config),
        "",
        "All teams",
        *_block(total, config),
    ]
    if by_team:
        text += ["", "Each team", *(_row_line(line, config) for line in by_team)]
    lines = [total, *by_team]

    weekly: list[str] = []
    for team in teams:
        team_rows = [row for row in attributed if team_of[row.author_id] == team]
        this_week, last_week = _weeks(shown[team], team_rows, days, config)
        if this_week.has_a_figure or last_week.has_a_figure:
            weekly += [
                _row_line(this_week, config, label=f"{shown[team]}, this week"),
                _row_line(last_week, config, label=f"{shown[team]}, last week"),
            ]
            lines += [this_week, last_week]
    if weekly:
        text += ["", "Each team, this week against last week", *weekly]

    coaching = sorted(
        user.name
        for user in users
        if user.team is not None and _coached(user, attributed, days, config)
    )
    if coaching:
        text += ["", "Coaching", *(f"  {name}" for name in coaching)]

    if unlisted:
        # A real signal and not clutter: notes by an author the directory does
        # not list are work nobody is accountable for, and the usual cause is a
        # directory that has gone stale. One line, and only when it is not zero.
        text += [
            "",
            f"{unlisted} notes are by people the directory does not list.",
        ]
    return _Parts(text, lines, everything, coaching=coaching)


def build_brief(
    role: Role,
    subject: User,
    *,
    users: Sequence[User],
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
) -> Brief | None:
    """The brief, or None when there is nothing to report.

    Pure: no I/O, no clock, no model. The route reads the store and the
    directory, chooses the window, and hands everything in -- so a brief can be
    tested by writing rows, which is the only way the "not sent" rule can be
    tested at all. `rows` may reach back before `since` (see `brief_window`):
    the rolling measures read only [since, until), the weeks their own days.

    `role` is what was ASKED FOR, not `subject.role`. A head of sales may want
    the rep view of one of their people, and the CRM asks for the brief it
    wants rather than being told what the subject's title entitles them to.
    """
    days = _days(since, until, config)
    if role is Role.REP:
        parts = _rep_brief(subject, rows, since, until, config, days)
    elif role is Role.TEAM_LEADER:
        parts = _team_brief(subject, users, rows, since, until, config, days)
    else:
        parts = _org_brief(subject, users, rows, since, until, config, days)

    # THE RULE THAT MATTERS. Not "no rows" -- no FIGURE: a brief whose every
    # measure is suppressed says nothing, however many rows it looked at. An id
    # list alone (signals, flagged people) is not a figure and sends nothing.
    if not any(line.has_a_figure for line in parts.lines):
        return None
    first_day, last_day = local_dates(since, until, config)
    return Brief(
        text="\n".join(parts.text) + "\n",
        first_day=first_day,
        last_day=last_day,
        days=config.rolling_window_days,
        lines=parts.lines,
        flagged_note_ids=_flagged(parts.covered),
        signals=parts.signals,
        flagged_yesterday=parts.flagged_yesterday,
        coaching=parts.coaching,
    )
