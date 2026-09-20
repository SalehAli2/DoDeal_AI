"""The three role briefs — text, from arithmetic, with no model call.

THREE VERSIONS, because there are three people who read one (register item
145): the rep's own work, the team leader's team, the head of sales across
teams. They are the same three measures rendered at three altitudes, not three
different sets of figures, so a rep and their manager can never be looking at
numbers that disagree.

NO MODEL TOUCHES A BRIEF. Every number is arithmetic from measures.py and every
sentence is a template in this file. A brief is read as fact -- it is the thing
a manager acts on before they have spoken to anyone -- and a model asked to
narrate figures will eventually produce one that is not in them. There is
nothing to gain here that is worth that: the sentences are short and there are
six of them.

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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.judgement_rows import JudgementRow
from dodeal_ai.units.structured_intelligence.measures import (
    BandMeasure,
    MeasureSuppressed,
    ShareMeasure,
    average_band,
    flagged_share,
    improved_share,
)
from dodeal_ai.units.structured_intelligence.user_directory import Role, User

# How wide a name column is before the figures start. Fixed rather than
# computed from the longest name, so two briefs a week apart line up in a
# terminal and in an email; a longer name simply pushes its own row out.
_NAME_WIDTH = 22


@dataclass(frozen=True, slots=True)
class BriefLine:
    """One named subject and its three measures, ready to render.

    The team's own roll-up and each person in it are the SAME shape, which is
    what keeps a team total and the rows under it from being computed two
    different ways.
    """

    name: str
    average_band: BandMeasure
    flagged_share: ShareMeasure
    improved_share: ShareMeasure

    @property
    def has_a_figure(self) -> bool:
        """Is there anything here to tell somebody? A line where all three are
        suppressed says nothing, and a brief of only those is not sent."""
        return (
            self.average_band.suppressed is None
            or self.flagged_share.suppressed is None
            or self.improved_share.suppressed is None
        )


def _line(name: str, rows: Sequence[JudgementRow], config: TenantConfig) -> BriefLine:
    """The three measures over one set of rows, under one name."""
    return BriefLine(
        name=name,
        average_band=average_band(rows, config),
        flagged_share=flagged_share(rows, config),
        improved_share=improved_share(rows, config),
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


def _row_line(line: BriefLine, config: TenantConfig) -> str:
    """One subject on one line, for the lists under a team or an org brief."""
    return (
        f"  {line.name:<{_NAME_WIDTH}}"
        f"typical {_band_phrase(line.average_band, config)}"
        f" · flagged {_flagged_phrase(line.flagged_share, config)}"
        f" · improved {_improved_phrase(line.improved_share, config)}"
    )


def _period(since: datetime, until: datetime, config: TenantConfig) -> str:
    """The window, in dates.

    `until` is exclusive, and in normal use it is "now" -- so nothing after it
    exists yet and naming its date is true. Stated rather than assumed, because
    a correct figure described with the wrong period is a wrong statement.
    """
    return f"{since:%Y-%m-%d} to {until:%Y-%m-%d} ({config.rolling_window_days} days)"


def _by_author(rows: Sequence[JudgementRow], author_id: int) -> list[JudgementRow]:
    return [row for row in rows if row.author_id == author_id]


def _rep_brief(
    subject: User,
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
) -> tuple[list[str], list[BriefLine]]:
    """The rep's own day. `rows` are already theirs -- the route narrows the
    store read by author, so nothing is filtered twice."""
    line = _line(subject.name, rows, config)
    text = [
        f"Note quality for {subject.name}.",
        _period(since, until, config),
        "",
        *_block(line, config),
    ]
    return text, [line]


def _team_brief(
    subject: User,
    users: Sequence[User],
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
) -> tuple[list[str], list[BriefLine]]:
    """The team leader's team: the team as a whole, then each person in it.

    The team's roll-up is computed over the team's rows POOLED, not averaged
    from the per-person figures. Averaging averages would weight a rep with two
    notes the same as one with forty, which is the arithmetic mistake a team
    figure is most often reported with.
    """
    if subject.team is None:
        # A team brief for somebody who is in no team. There is no team to
        # report on, and inventing one would be worse than saying nothing: no
        # lines means no figures, which build_brief turns into a 204.
        return [], []

    # The leader is in their own team and appears in the list. They write notes
    # too, and a team figure their own work is inside should say so.
    members = [user for user in users if user.team == subject.team]
    member_ids = {user.user_id for user in members}
    team_rows = [row for row in rows if row.author_id in member_ids]

    team = _line(f"Team {subject.team}", team_rows, config)
    people = [
        _line(user.name, _by_author(team_rows, user.user_id), config)
        for user in sorted(members, key=lambda user: user.name)
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
    return text, [team, *people]


def _org_brief(
    subject: User,
    users: Sequence[User],
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
) -> tuple[list[str], list[BriefLine]]:
    """The head of sales, across teams. Teams, not people: naming every rep in
    the company is a list nobody reads, and the team leader's brief already
    does it for the people it is about."""
    team_of = {user.user_id: user.team for user in users if user.team is not None}
    teams = sorted({team for team in team_of.values()})

    everything = [row for row in rows if row.author_id in team_of]
    unattributed = len(rows) - len(everything)

    total = _line("All teams", everything, config)
    by_team = [
        _line(
            team,
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
    if unattributed:
        # A real signal and not clutter: notes by an author the directory does
        # not list are work nobody is accountable for, and the usual cause is a
        # directory that has gone stale. One line, and only when it is not zero.
        text += [
            "",
            f"{unattributed} notes are by people the directory does not list.",
        ]
    return text, [total, *by_team]


def build_brief(
    role: Role,
    subject: User,
    *,
    users: Sequence[User],
    rows: Sequence[JudgementRow],
    since: datetime,
    until: datetime,
    config: TenantConfig,
) -> str | None:
    """The brief as text, or None when there is nothing to report.

    Pure: no I/O, no clock, no model. The route reads the store and the
    directory, chooses the window, and hands everything in -- so a brief can be
    tested by writing rows, which is the only way the "not sent" rule can be
    tested at all.

    `role` is what was ASKED FOR, not `subject.role`. A head of sales may want
    the rep view of one of their people, and the CRM asks for the brief it
    wants rather than being told what the subject's title entitles them to.
    """
    if role is Role.REP:
        text, lines = _rep_brief(subject, rows, since, until, config)
    elif role is Role.TEAM_LEADER:
        text, lines = _team_brief(subject, users, rows, since, until, config)
    else:
        text, lines = _org_brief(subject, users, rows, since, until, config)

    # THE RULE THAT MATTERS. Not "no rows" -- no FIGURE: a brief whose every
    # measure is suppressed says nothing, however many rows it looked at.
    if not any(line.has_a_figure for line in lines):
        return None
    return "\n".join(text) + "\n"
