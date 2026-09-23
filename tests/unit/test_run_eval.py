"""The eval runner's arithmetic: what agrees, what falls, and what is excluded.

A FAKE MODEL AND THE INVENTED FIXTURE. Every test here scripts `FakeLLM` with
the answers it wants and runs the real `evaluate` over item 138's five invented
notes -- so what is under test is the comparison and the exclusion rule, not a
provider. No network call, no cost, and the figures are exact integers rather
than something that drifts with a model.

THE THREE THINGS THAT MUST HOLD, and the reason each is here:

  agreement is 100 % when the fake answers the marks   -- otherwise the
      comparison itself is wrong, and every figure it reports is noise.
  it falls by exactly one note when one answer changes -- otherwise the figure
      is insensitive to the thing it exists to measure.
  an unmarked note is EXCLUDED and never counted       -- otherwise unfinished
      marking is silently reported as agreement, which is the one failure that
      would be quoted to the business as a result.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from dodeal_ai.core.config import get_settings
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.eval_set import (
    REPO_ROOT,
    EvalRow,
    load_eval_set,
)
from dodeal_ai.units.structured_intelligence.schemas import CheckName, NoteType
from dodeal_ai.units.structured_intelligence.scoring import (
    SCORE_TEMPLATE,
    applicable_checks,
)
from dodeal_ai.units.structured_intelligence.vague import template_for
from scripts.diagnose_notes import RecordingClient, build_scope, output_path, write_csv
from scripts.run_eval import (
    Summary,
    Tally,
    _report,
    disagreement_rows,
    evaluate,
    language_of,
    summarise,
)
from tests.helpers.fake_llm import FakeLLM, json_response

FIXTURE = Path("tests/fixtures/eval_set/invented_notes.jsonl")
CONFIG = get_tenant_config("tenant-a")

# The types the fake classifies each note as when it is agreeing with the
# marks. inv-04 is unmarked, so nothing it is classified as can agree or
# disagree; discovery is chosen because it exercises the most checks.
AGREEING_TYPES: dict[str, NoteType] = {
    "inv-01": NoteType.DISCOVERY,
    "inv-02": NoteType.NO_CONTACT,
    "inv-03": NoteType.VIEWING,
    "inv-04": NoteType.DISCOVERY,
    "inv-05": NoteType.NEGOTIATION,
}


@pytest.fixture
def rows(tmp_path) -> list[EvalRow]:
    """The invented fixture, copied out of the repo so the loader will read it."""
    assert not tmp_path.resolve().is_relative_to(REPO_ROOT)
    destination = tmp_path / FIXTURE.name
    shutil.copyfile(FIXTURE, destination)
    return load_eval_set(destination)


def _vague_answer(row: EvalRow) -> dict[str, object]:
    """The vague payload that AGREES with this row's marks.

    An unmarked row has nothing to agree with, so it gets a valid non-vague
    answer: the schema's biconditional means "not vague" must carry no
    components and no prompt, and any other shape would be malformed output
    rather than a disagreement.
    """
    if row.is_vague is None or not row.is_vague:
        return {
            "is_vague": False,
            "missing_components": [],
            "clarification_prompt": None,
            "reasoning": "invented",
        }
    return {
        "is_vague": True,
        "missing_components": [c.value for c in (row.missing_components or [])],
        "clarification_prompt": "What is the next step and when?",
        "reasoning": "invented",
    }


def _checks_answer(
    note_type: NoteType, row: EvalRow, flip: frozenset[CheckName] = frozenset()
) -> dict[str, object]:
    """The score payload for `note_type`, agreeing with the row's marks.

    Exactly the checks this type asks for -- `validate_checks` refuses any
    other set, so a helper that answered all twelve would be testing the
    reprompt path instead of the comparison. `flip` inverts named answers, which
    is how a test makes one note disagree without touching anything else.
    """
    marks = row.checks or {}
    answers = {
        check.value: (marks.get(check, False) is not (check in flip))
        for check in applicable_checks(note_type, CONFIG)
    }
    return {"checks": answers, "reasoning": "invented"}


def _script(
    llm: FakeLLM,
    rows: list[EvalRow],
    *,
    types: dict[str, NoteType] | None = None,
    flip: dict[str, frozenset[CheckName]] | None = None,
) -> None:
    """Queue one classify, one vague and one score answer per note, in order.

    `script_for` queues by TEMPLATE and the notes run one at a time, so a
    template two notes share pops their answers in note order -- which is what
    lets this script a whole corpus without knowing the gathered passes' order.
    """
    chosen = {**AGREEING_TYPES, **(types or {})}
    flips = flip or {}
    for row in rows:
        note_type = chosen[row.id]
        llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": note_type.value}))
        llm.script_for(template_for(note_type), json_response(_vague_answer(row)))
        llm.script_for(
            SCORE_TEMPLATE,
            json_response(
                _checks_answer(note_type, row, flips.get(row.id, frozenset()))
            ),
        )


async def _run(rows: list[EvalRow], llm: FakeLLM) -> Summary:
    recorder = RecordingClient(llm)
    runs = await evaluate(
        recorder,
        rows,
        scope=build_scope("tenant-a"),
        config=CONFIG,
        settings=get_settings(),
        recorder=recorder,
    )
    return summarise(list(zip(rows, runs, strict=True)), CONFIG)


async def test_agreement_is_total_when_the_fake_answers_the_marks(rows):
    """Every figure is 100 %, over exactly the notes a human has marked."""
    llm = FakeLLM()
    _script(llm, rows)
    summary = await _run(rows, llm)

    assert summary.failed == 0
    # inv-04 is unmarked for all four note-level figures, so each counts four.
    for figure in ("classify", "is_vague", "components"):
        assert summary.overall[figure].share == 100.0, figure
        assert (summary.overall[figure].matched, summary.overall[figure].counted) == (
            4,
            4,
        ), figure
    # 9 + 5 + 9 check answers: inv-03 and inv-04 carry no marked checks.
    assert (summary.overall["checks"].matched, summary.overall["checks"].counted) == (
        23,
        23,
    )
    # Three notes carry both a type and a full set of checks, so three bands.
    assert (summary.overall["band"].matched, summary.overall["band"].counted) == (3, 3)
    assert summary.overall["band"].share == 100.0
    assert summary.band_not_computable == 0
    assert disagreement_rows(summary) == []


def test_a_tally_over_nothing_has_no_share():
    """100 % of nothing and 0 % of nothing are both figures nobody may quote."""
    assert Tally().share is None
    assert Tally(matched=1, counted=2).share == 50.0


async def test_the_unmarked_note_is_excluded_and_never_counted(rows):
    """inv-04 has no marks, so it enters no denominator of any figure."""
    llm = FakeLLM()
    _script(llm, rows)
    summary = await _run(rows, llm)

    unmarked = next(o for o in summary.outcomes if o.row_id == "inv-04")
    assert all(counted == 0 for _, counted in unmarked.figures.values())
    assert unmarked.disagreements == []
    # It is still a note that ran: it has a type bucket and a language, and its
    # latency and tokens are in the per-pass figures.
    assert unmarked.note_type == NoteType.DISCOVERY.value
    assert summary.notes == 5
    assert summary.passes[0].notes == 5


async def test_classify_agreement_falls_by_one_note_when_one_type_changes(rows):
    """One wrong classification is one note off the classify figure, and no more."""
    llm = FakeLLM()
    _script(llm, rows, types={"inv-01": NoteType.CALLBACK})
    summary = await _run(rows, llm)

    assert (
        summary.overall["classify"].matched,
        summary.overall["classify"].counted,
    ) == (
        3,
        4,
    )
    assert summary.overall["classify"].share == 75.0
    # callback and discovery suppress the same components, so the denominator
    # does not move and the band is unaffected. A wrong type is only a wrong
    # BAND when it changes which components apply -- which is exactly why the
    # two figures are reported separately.
    assert (summary.overall["band"].matched, summary.overall["band"].counted) == (3, 3)

    rows_out = disagreement_rows(summary)
    assert [(r["id"], r["figure"], r["expected"], r["actual"]) for r in rows_out] == [
        ("inv-01", "classify", "discovery", "callback")
    ]


async def test_the_band_figure_falls_when_a_note_is_marked_differently(rows):
    """Flipping two checks moves one note's band, and the headline follows."""
    llm = FakeLLM()
    # inv-01 is marked 73/100 -> good. Turning what_happened's two checks off
    # takes 25 marks out of an 80 denominator, which is a different band.
    _script(llm, rows, flip={"inv-01": frozenset({CheckName.WH_OUTCOME})})
    summary = await _run(rows, llm)

    assert (summary.overall["checks"].matched, summary.overall["checks"].counted) == (
        22,
        23,
    )
    assert (summary.overall["band"].matched, summary.overall["band"].counted) == (2, 3)
    assert summary.overall["band"].share == pytest.approx(66.67, abs=0.01)

    figures = {r["figure"] for r in disagreement_rows(summary)}
    assert figures == {"check.wh_outcome", "band"}


async def test_a_marked_note_whose_pass_did_not_run_is_a_disagreement(rows, tmp_path):
    """A note the gate suppresses disagrees with its marks; it is not excluded."""
    short = tmp_path / "short.jsonl"
    short.write_text(
        '{"id": "s-1", "text": "ok", "lead": {}, "note_type": "discovery", '
        '"is_vague": true, "missing_components": ["what_happened"]}\n',
        encoding="utf-8",
    )
    loaded = load_eval_set(short)

    llm = FakeLLM()  # nothing scripted: any call at all would raise
    summary = await _run(loaded, llm)

    assert summary.outcomes[0].figures["classify"] == (0, 1)
    assert summary.outcomes[0].figures["is_vague"] == (0, 1)
    assert ("classify", "discovery", "-") in summary.outcomes[0].disagreements
    assert llm.call_count == 0


async def test_the_breakdowns_split_by_note_type_and_by_script(rows):
    """Each note lands in one type bucket and one language bucket."""
    llm = FakeLLM()
    _script(llm, rows)
    summary = await _run(rows, llm)

    assert set(summary.by_language) == {"english", "arabic", "mixed"}
    assert summary.by_language["arabic"]["classify"].counted == 1
    assert summary.by_language["mixed"]["classify"].counted == 1
    assert summary.by_language["english"]["classify"].counted == 2

    assert set(summary.by_type) == {
        NoteType.DISCOVERY.value,
        NoteType.NO_CONTACT.value,
        NoteType.VIEWING.value,
        NoteType.NEGOTIATION.value,
    }
    # inv-01 and inv-04 both bucket as discovery; only inv-01 is marked.
    assert summary.by_type[NoteType.DISCOVERY.value]["classify"].counted == 1


async def test_a_scored_row_whose_marks_do_not_fit_its_type_is_reported(tmp_path):
    """A marking error leaves the band figure and is counted, never hidden."""
    bad = tmp_path / "bad.jsonl"
    # no_contact asks five checks; these marks answer a discovery note's nine,
    # so compute_score refuses them and no expected band exists.
    bad.write_text(
        '{"id": "b-1", "text": "An invented note that is long enough to pass.", '
        '"lead": {}, "note_type": "no_contact", '
        '"checks": {"wh_outcome": true, "wh_action": true}}\n',
        encoding="utf-8",
    )
    loaded = load_eval_set(bad)

    llm = FakeLLM()
    _script(llm, loaded, types={"b-1": NoteType.NO_CONTACT})
    summary = await _run(loaded, llm)

    assert summary.overall["band"].counted == 0
    assert summary.overall["band"].share is None
    assert summary.band_not_computable == 1


async def test_a_marking_error_logs_no_model_failure(tmp_path, caplog):
    """Register item 140: a human's bad mark never writes output_validation_failed.

    The row answers all five checks no_contact asks AND `wh_outcome`, whose
    component the type suppresses -- the exact shape `validate_checks` refuses.
    Routed through `compute_score` that refusal logs the event that means a
    MODEL answered badly, and an alert built on it would spike every time
    somebody ran the eval over a half-marked set.
    """
    bad = tmp_path / "suppressed_check.jsonl"
    bad.write_text(
        '{"id": "nc-1", "text": "An invented note that is long enough to pass.", '
        '"lead": {}, "note_type": "no_contact", '
        '"checks": {"ns_action": true, "ns_date": true, "ns_closure": false, '
        '"cl_readable": true, "cl_substance": true, "wh_outcome": true}}\n',
        encoding="utf-8",
    )
    loaded = load_eval_set(bad)

    llm = FakeLLM()
    _script(llm, loaded, types={"nc-1": NoteType.NO_CONTACT})
    with caplog.at_level(logging.WARNING, logger="dodeal_ai.unit_a"):
        summary = await _run(loaded, llm)

    # Excluded and counted, exactly as any other marking error.
    assert summary.overall["band"].counted == 0
    assert summary.band_not_computable == 1
    assert [
        r for r in caplog.records if "output_validation_failed" in r.getMessage()
    ] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Called the client this morning", "english"),
        ("اتصلت بالعميل اليوم", "arabic"),
        ("عاينا الوحدة and he liked it", "mixed"),
        ("12/10 15:30", "english"),
    ],
)
def test_the_language_bucket_is_decided_by_script(text: str, expected: str):
    """The same Arabic pattern the fixed clarification question uses."""
    assert language_of(text) == expected


async def test_the_band_line_is_the_last_line_of_the_report(rows, capsys):
    """The headline is printed last and on its own line, as the business reads it."""
    llm = FakeLLM()
    _script(llm, rows)
    summary = await _run(rows, llm)
    _report(summary)

    printed = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert printed[-1].startswith("BAND AGREEMENT:")
    assert "100.0%" in printed[-1]
    # And nothing else on the line: a band figure sharing a line with a latency
    # is a band figure somebody quotes wrong.
    assert printed[-1].count("BAND AGREEMENT") == 1
    assert sum(line.startswith("BAND AGREEMENT") for line in printed) == 1


def test_the_disagreement_csv_is_written_beside_the_input(tmp_path):
    """The CSV lands next to the scored set, never in the repo, and reads back."""
    source = tmp_path / "set.jsonl"
    destination = output_path(source, "disagreements")
    assert destination.parent == source.parent
    assert not destination.resolve().is_relative_to(REPO_ROOT)

    write_csv(destination, [{"id": "inv-01", "figure": "classify"}])
    assert destination.read_text(encoding="utf-8").splitlines()[0] == "id,figure"


# --- the two sides of the vague figure (register item 149) -------------------


def test_the_vague_figure_is_split_into_caught_and_false_alarms(rows, capsys):
    """Of the human-vague notes, how many were marked vague; of the human-clear
    ones, how many were -- each printed with its count."""
    from scripts.diagnose_notes import PassRun

    marked = [row for row in rows if row.is_vague is not None]
    vague = [row for row in marked if row.is_vague]
    clear = [row for row in marked if not row.is_vague]
    assert vague and clear
    runs = {
        # Every vague note caught but the first; every clear note left clear
        # but the first, which the pipeline wrongly marks vague.
        **{
            row.id: PassRun(row_id=row.id, is_vague=row is not vague[0])
            for row in vague
        },
        **{row.id: PassRun(row_id=row.id, is_vague=row is clear[0]) for row in clear},
    }
    pairs = [(row, runs.get(row.id, PassRun(row_id=row.id))) for row in rows]

    summary = summarise(pairs, CONFIG)

    assert (summary.vague_caught.matched, summary.vague_caught.counted) == (
        len(vague) - 1,
        len(vague),
    )
    assert (
        summary.clear_marked_vague.matched,
        summary.clear_marked_vague.counted,
    ) == (1, len(clear))
    _report(summary)
    out = capsys.readouterr().out
    assert f"human-vague marked vague  {len(vague) - 1}/{len(vague)}" in out
    assert f"human-clear marked vague  1/{len(clear)}" in out


def test_a_pass_that_did_not_run_is_not_marked_vague(rows):
    """No answer is not a vague mark: it counts against the caught share."""
    from scripts.diagnose_notes import PassRun

    pairs = [(row, PassRun(row_id=row.id)) for row in rows]
    summary = summarise(pairs, CONFIG)
    assert summary.vague_caught.matched == 0
    assert summary.clear_marked_vague.matched == 0
