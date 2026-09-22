"""The three role briefs: what they say, and when they are not sent at all.

THE RULE THE PIECE IS FOR is `test_a_brief_with_nothing_to_report_is_not_sent`
and the tests around it. An empty daily email trains people to ignore the
channel within a fortnight, so a brief whose every measure is suppressed is
None here and 204 on the route. "Nothing to report" is about FIGURES, not
rows: a brief that looked at eight notes and can say nothing about any of them
is still an empty email.

THE SECOND CLAIM IS THAT NO SUPPRESSED MEASURE IS EVER RENDERED AS A NUMBER.
A rep below the floor reads "not enough yet (4 of 10)" -- never "poor" and
never "0%". The tests assert on the rendered TEXT rather than on the measure,
because the measure being right and the template printing something else is a
real and easy failure.

Rows and people are invented and built in code. A lowered evidence floor keeps
the tables small enough to read; the shipped floor of 10 has its own test in
tests/unit/test_measures.py.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from dodeal_ai.units.structured_intelligence.brief import build_brief
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.judgement_rows import JudgementRow
from dodeal_ai.units.structured_intelligence.user_directory import Role, User

CONFIG = dataclasses.replace(get_tenant_config("tenant-a"), measure_evidence_floor=3)
SINCE = datetime(2026, 8, 21, tzinfo=UTC)
UNTIL = datetime(2026, 9, 20, tzinfo=UTC)

HEAD = User(user_id=401, name="Ayla Brook", role=Role.HEAD_OF_SALES, team=None)
LEADER = User(user_id=402, name="Hana Reyes", role=Role.TEAM_LEADER, team="north")
IDRIS = User(user_id=501, name="Idris Vale", role=Role.REP, team="north")
NOOR = User(user_id=502, name="Noor Adeyemi", role=Role.REP, team="north")
SOUTH_LEADER = User(
    user_id=403, name="Teo Lindqvist", role=Role.TEAM_LEADER, team="south"
)
SANA = User(user_id=503, name="Sana Okafor", role=Role.REP, team="south")

EVERYONE = [HEAD, LEADER, IDRIS, NOOR, SOUTH_LEADER, SANA]


def _row(
    *,
    author_id: int,
    note_id: int,
    total: int | None = 72,
    flagged: bool = False,
    prompt_sent: bool = False,
) -> JudgementRow:
    scored = total is not None
    return JudgementRow.model_validate(
        {
            "note_id": note_id,
            "lead_id": 9000 + note_id,
            "author_id": author_id,
            "note_created_at": "2026-09-01T09:00:00+00:00",
            "note_type": "discovery" if scored else None,
            "band": CONFIG.band_for(total).value if scored else None,
            "total": total if scored else None,
            "denominator": 80 if scored else None,
            "suppressed_reason": None if scored else "insufficient_evidence",
            "prompt_sent": prompt_sent,
            "enforcement_verdict": "flag" if flagged else "allow",
            "rubric_version": "note_rubric_v2",
            "prompt_version": "unit_a_prompts_v2",
            "model_version": "invented-model-1",
            "config_version": "tenant-cfg-default-4",
        }
    )


def _rows(author_id: int, count: int, *, first_note: int = 1, **kwargs: object):
    return [
        _row(author_id=author_id, note_id=first_note + index, **kwargs)  # type: ignore[arg-type]
        for index in range(count)
    ]


def _brief(role: Role, subject: User, rows, users=EVERYONE) -> str | None:
    """The brief's text, the part these tests read (register item 154 made
    the brief a Brief; its JSON has tests of its own)."""
    brief = build_brief(
        role,
        subject,
        users=users,
        rows=rows,
        since=SINCE,
        until=UNTIL,
        config=CONFIG,
    )
    return None if brief is None else brief.text


# --- not sent ---------------------------------------------------------------


def test_a_brief_with_nothing_to_report_is_not_sent() -> None:
    """THE RULE. No rows means no figures, and no figures means no email."""
    assert _brief(Role.REP, IDRIS, []) is None


def test_a_brief_of_only_suppressed_measures_is_not_sent() -> None:
    """Not "no rows" -- no FIGURE. Two notes is below the floor of three, so
    every measure is a wait, and "not enough evidence yet, three times" is an
    empty email with extra words in it."""
    assert _brief(Role.REP, IDRIS, _rows(501, 2)) is None


def test_one_figure_is_enough_to_send() -> None:
    """At the floor the average and the flagged share both report, so there is
    something to say even though nothing was ever asked about."""
    text = _brief(Role.REP, IDRIS, _rows(501, 3))
    assert text is not None
    assert "nothing was asked about" in text


def test_a_team_leader_with_no_team_gets_nothing() -> None:
    """There is no team to report on, and inventing one would be worse than
    saying nothing."""
    assert _brief(Role.TEAM_LEADER, HEAD, _rows(501, 20)) is None


def test_a_team_with_nobody_in_it_gets_nothing() -> None:
    lonely = User(
        user_id=404, name="Rune Castellan", role=Role.TEAM_LEADER, team="west"
    )
    assert (
        _brief(Role.TEAM_LEADER, lonely, _rows(501, 20), users=[lonely, HEAD]) is None
    )


# --- no suppressed measure is ever a number ---------------------------------


def test_a_rep_below_the_floor_reads_as_a_wait_and_never_as_a_band() -> None:
    """The failure this prevents: "poor" or "0%" off four notes, which a
    manager reads as a standard and the rep cannot argue with."""
    text = _brief(
        Role.REP,
        IDRIS,
        _rows(501, 3) + _rows(501, 1, first_note=90, flagged=True, prompt_sent=True),
    )
    assert text is not None
    assert "Improved after asking  not enough yet (1 of 3)" in text
    for word in ("poor", "0%"):
        assert word not in text.split("Improved after asking")[1]


def test_a_rep_with_no_scored_notes_is_told_so() -> None:
    """Distinct from "not enough yet": there is nothing at all, which is a
    different thing to read."""
    text = _brief(
        Role.REP, IDRIS, _rows(501, 4, total=None, flagged=True, prompt_sent=True)
    )
    assert text is not None
    assert "Typical note           no scored notes" in text


# --- the rep brief ----------------------------------------------------------


def test_the_rep_brief_names_the_person_and_the_period() -> None:
    """Names, not ids, and the period the figures were actually computed over
    -- a correct figure with the wrong period is a wrong statement."""
    text = _brief(Role.REP, IDRIS, _rows(501, 4))
    assert text is not None
    assert text.startswith("Note quality for Idris Vale.\n")
    assert "2026-08-21 to 2026-09-20 (30 days)" in text
    assert "501" not in text


def test_the_rep_brief_carries_the_three_measures() -> None:
    rows = _rows(501, 3) + _rows(501, 1, first_note=90, flagged=True)
    text = _brief(Role.REP, IDRIS, rows)
    assert text is not None
    assert "Typical note           good over 4 notes" in text
    assert "Flagged                1 of 4 (25%)" in text


def test_the_rep_brief_ends_with_a_newline() -> None:
    """It is an email body and a terminal line alike; a file without one is a
    small wrongness in both."""
    text = _brief(Role.REP, IDRIS, _rows(501, 4))
    assert text is not None and text.endswith("\n")


# --- the team brief ---------------------------------------------------------


def test_the_team_brief_rolls_the_team_up_and_then_names_each_person() -> None:
    rows = _rows(501, 4, flagged=True) + _rows(502, 4, first_note=20)
    text = _brief(Role.TEAM_LEADER, LEADER, rows)
    assert text is not None
    assert text.startswith("Note quality for Hana Reyes's team (north).\n")
    assert "Flagged                4 of 8 (50%)" in text
    assert "Each person" in text
    assert "Idris Vale" in text and "Noor Adeyemi" in text


def test_the_team_total_is_pooled_and_not_an_average_of_averages() -> None:
    """A rep with three notes must not weigh the same as one with nine. Pooled,
    the team's mean is 41 (fair); averaging the two averages would give 55."""
    rows = _rows(501, 9, total=30) + _rows(502, 3, total=75, first_note=20)
    text = _brief(Role.TEAM_LEADER, LEADER, rows)
    assert text is not None
    assert "Typical note           fair over 12 notes" in text


def test_a_team_brief_leaves_out_other_teams() -> None:
    rows = _rows(501, 4) + _rows(503, 4, first_note=40)
    text = _brief(Role.TEAM_LEADER, LEADER, rows)
    assert text is not None
    assert "over 4 notes" in text
    assert "Sana Okafor" not in text


def test_the_leader_is_in_their_own_team_list() -> None:
    """They write notes too, and a team figure their own work is inside should
    say so."""
    rows = _rows(402, 4) + _rows(501, 4, first_note=20)
    text = _brief(Role.TEAM_LEADER, LEADER, rows)
    assert text is not None
    assert "Hana Reyes" in text.split("Each person")[1]


# --- the head-of-sales brief ------------------------------------------------


def test_the_org_brief_is_teams_and_not_people() -> None:
    """Naming every rep in the company is a list nobody reads; the team
    leader's brief already does that for the people it is about."""
    rows = _rows(501, 4) + _rows(503, 4, first_note=40)
    text = _brief(Role.HEAD_OF_SALES, HEAD, rows)
    assert text is not None
    assert text.startswith("Note quality across all teams, for Ayla Brook.\n")
    assert "north" in text and "south" in text
    assert "Idris Vale" not in text


def test_the_org_total_covers_every_team() -> None:
    rows = _rows(501, 4, flagged=True) + _rows(503, 4, first_note=40)
    text = _brief(Role.HEAD_OF_SALES, HEAD, rows)
    assert text is not None
    assert "Flagged                4 of 8 (50%)" in text


def test_notes_by_people_the_directory_does_not_list_are_reported() -> None:
    """A real signal, not clutter: the usual cause is a stale directory, and a
    figure quietly computed over three quarters of the notes is worse."""
    rows = _rows(501, 4) + _rows(777, 3, first_note=60)
    text = _brief(Role.HEAD_OF_SALES, HEAD, rows)
    assert text is not None
    assert "3 notes are by people the directory does not list." in text
    assert "over 4 notes" in text


def test_nothing_is_said_when_every_note_is_attributed() -> None:
    text = _brief(Role.HEAD_OF_SALES, HEAD, _rows(501, 4))
    assert text is not None
    assert "does not list" not in text


# --- the role asked for, not the subject's title ----------------------------


def test_the_role_asked_for_wins_over_the_subjects_own() -> None:
    """A head of sales may want the rep view of one of their people, and the
    CRM asks for the brief it wants."""
    rows = _rows(402, 4)
    text = _brief(Role.REP, LEADER, rows)
    assert text is not None
    assert text.startswith("Note quality for Hana Reyes.\n")
    assert "Each person" not in text


@pytest.mark.parametrize("role", list(Role))
def test_no_brief_ever_prints_an_id(role: Role) -> None:
    """The whole reason the directory is an ask: a brief that printed ids would
    be forwarded to somebody who had to go and look every one of them up."""
    subject = {Role.REP: IDRIS, Role.TEAM_LEADER: LEADER, Role.HEAD_OF_SALES: HEAD}[
        role
    ]
    rows = _rows(501, 4, flagged=True) + _rows(502, 4, first_note=20)
    text = _brief(role, subject, rows)
    assert text is not None
    for user in EVERYONE:
        assert str(user.user_id) not in text


# --- the flagged note ids (register item 154) --------------------------------


def test_flagged_ids_are_newest_first_distinct_and_at_most_fifty() -> None:
    """Newest note first, a later-recorded row first on a tie, each id once."""
    from dodeal_ai.units.structured_intelligence.brief import (
        MAX_NOTE_IDS,
        newest_note_ids,
    )

    rows = [
        _row(author_id=501, note_id=n, flagged=True).model_copy(
            update={
                "note_created_at": datetime(2026, 9, 1, tzinfo=UTC).replace(
                    minute=n % 60
                )
            }
        )
        for n in range(1, 60)
    ]
    ids = newest_note_ids([*rows, rows[0]])
    assert len(ids) == MAX_NOTE_IDS
    assert ids[0] == 59
    assert len(set(ids)) == len(ids)
