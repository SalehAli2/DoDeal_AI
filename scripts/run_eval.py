"""The eval runner — does the AI agree with a human, and where does it not.

THE BAND FIGURE IS THE HEADLINE, and it is printed last, on its own line,
because it is the only number the business agreed to accept the unit on. Every
other figure in this report exists to explain it: a band is computed from check
answers, the check answers are asked for the classified type, and the type is
pass one. A band figure that fell with no per-pass breakdown beside it would
say that something is wrong and nothing about what.

WHAT IS EXCLUDED, AND WHY EXCLUSION IS NOT A SOFT FAILURE. A note nobody has
marked for a pass is EXCLUDED from that pass's figure: it leaves the
denominator, it is never counted as agreement, and the note count is printed
beside every share so a figure over three notes cannot be read as a figure over
fifty. A runner that scored the pipeline against an absent mark would report
agreement with nobody.

A MARKED NOTE WHOSE PASS DID NOT RUN IS A DISAGREEMENT, not an exclusion. If a
human says `discovery` and the length gate suppressed the note, the pipeline
and the human disagree about that note -- and it is exactly the kind of
disagreement a gate threshold change is meant to fix, so hiding it would hide
the reason to look.

THE BAND IS COMPUTED FROM THE MARKS, NEVER READ FROM THEM. The scored set holds
no band field (eval_set.py); the expected band is `compute_score` applied to
the human's check answers under the human's note type, and the actual band is
the same function applied to the model's. Both sides go through one piece of
arithmetic, so a disagreement is about the ANSWERS and never about two
different ways of adding them up. A row whose marks do not match its type's
applicable set cannot be put through that arithmetic; it is excluded and
COUNTED AS EXCLUDED on the terminal, because it is a marking error somebody
should fix rather than a quiet gap.

Usage:
    uv run python -m scripts.run_eval             # dry run, sends nothing
    uv run python -m scripts.run_eval --live      # real paid calls

Requires DODEAL_EVAL_SET_PATH, outside this repository. The per-note
disagreement CSV is written beside it.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, build_llm_client
from dodeal_ai.core.llm.profiles import (
    PROFILE_UNIT_A_CLASSIFY,
    PROFILE_UNIT_A_SCORE,
    PROFILE_UNIT_A_VAGUE,
)
from dodeal_ai.units.structured_intelligence.config import (
    TenantConfig,
    get_tenant_config,
)
from dodeal_ai.units.structured_intelligence.eval_set import EvalRow
from dodeal_ai.units.structured_intelligence.pipeline import _ARABIC_SCRIPT_PATTERN
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import (
    applicable_checks,
    compute_score,
)
from scripts.diagnose_notes import (
    DEFAULT_ASSUMED_TYPE,
    DEFAULT_TENANT,
    PassRun,
    RecordingClient,
    _guard_against_accidental_live_call,
    _refuse_an_oversized_run,
    build_scope,
    load_rows_or_exit,
    output_path,
    print_prompts,
    run_passes,
    use_utf8_output,
    write_csv,
)

# The band agreement the business accepts the unit on, printed beside the
# figure. REPORTED, NEVER ASSERTED: this script has no exit code that depends
# on it, because a target that failed a run would turn a measurement into a
# gate and make people stop running it.
BAND_TARGET_PERCENT = 85

# The other half of "which language is this note", beside pipeline.py's Arabic
# pattern. Latin letters only -- this decides a three-way bucket for a report,
# not a linguistic fact. A wrong value mislabels a bucket and changes no
# judgement anywhere.
_LATIN_SCRIPT_PATTERN = re.compile("[A-Za-z]")

# The note-type bucket for a note nobody typed by hand and that never reached
# the classifier. Named rather than blank so a reader does not take an empty
# cell for a missing row.
_UNKNOWN_TYPE = "(unknown)"

# The five figures, in the order they are reported. `checks` is counted over
# CHECK ANSWERS and the other four over NOTES, which is why every figure prints
# its own denominator rather than one note count for the row.
_FIGURES: tuple[str, ...] = ("classify", "is_vague", "components", "checks", "band")

_PASSES: tuple[tuple[str, str], ...] = (
    ("classify", PROFILE_UNIT_A_CLASSIFY),
    ("vague", PROFILE_UNIT_A_VAGUE),
    ("score", PROFILE_UNIT_A_SCORE),
)


def language_of(text: str) -> str:
    """arabic, english or mixed, decided by script.

    The Arabic half is pipeline.py's own pattern -- imported, not copied, so
    "does this note contain Arabic" has ONE answer in this repo and the report
    cannot disagree with the fixed clarification question about it.

    Text with neither script (digits and punctuation only) reads as english.
    It is the residual bucket, not a claim about the note; nothing downstream
    branches on it.
    """
    arabic = bool(_ARABIC_SCRIPT_PATTERN.search(text))
    latin = bool(_LATIN_SCRIPT_PATTERN.search(text))
    if arabic and latin:
        return "mixed"
    return "arabic" if arabic else "english"


@dataclass(slots=True)
class Tally:
    """How many agreed, out of how many were counted at all."""

    matched: int = 0
    counted: int = 0

    def add(self, matched: int, counted: int) -> None:
        self.matched += matched
        self.counted += counted

    @property
    def share(self) -> float | None:
        """The percentage, or None when nothing was counted.

        None rather than 0.0 or 100.0: a figure over an empty set is not a
        figure, and both of those numbers would be read as one.
        """
        return None if self.counted == 0 else 100.0 * self.matched / self.counted


@dataclass(slots=True)
class NoteOutcome:
    """One note's contribution to every figure, plus what it disagreed on."""

    row_id: str
    language: str
    note_type: str
    figures: dict[str, tuple[int, int]] = field(default_factory=dict)
    # (figure, expected, actual) -- fixed vocabulary on both sides. A note's
    # TEXT is never here: the id is what joins a disagreement back to the note,
    # and the note lives in the operator's own file.
    disagreements: list[tuple[str, str, str]] = field(default_factory=list)
    band_not_computable: bool = False


def _expected_band(row: EvalRow, config: TenantConfig) -> str | None:
    """The band the human's marks imply, or None when they cannot imply one.

    Needs BOTH a type and a full set of check answers, because the denominator
    is chosen by the type: the same twelve answers are 100 under discovery and
    35 under no_contact.

    THE MISMATCH IS TESTED HERE, NOT CAUGHT FROM `compute_score` (register item
    140). Its refusal goes through `output_rejected`, which writes
    `output_validation_failed` -- the event that means A MODEL ANSWERED BADLY,
    and the one an alert on the unit's health is built from. A human who marked
    nine discovery checks on a no_contact row is not a model failure, and
    letting that row raise put a spike on that alert every time somebody ran
    the eval over a half-marked set. Comparing the two sets first costs one set
    build and keeps the event meaning what it says.

    A mismatch is a marking error rather than a disagreement, so it returns
    None and the caller reports the exclusion.
    """
    if row.checks is None or not isinstance(row.note_type, NoteType):
        return None
    if set(row.checks) != set(applicable_checks(row.note_type, config)):
        return None
    return compute_score(row.checks, row.note_type, config).band.value


def compare(row: EvalRow, run: PassRun, config: TenantConfig) -> NoteOutcome:
    """One note's marks against one note's run, figure by figure.

    Every figure is `(matched, counted)`. `counted` is 0 where the human has
    not marked it -- that is the exclusion, and it is the ONLY thing that
    removes a note from a denominator. A pass that did not run still counts
    against a marked note, because the human marked it and the pipeline did not
    answer.

    The note-type bucket is the HUMAN's type where there is one and the
    classifier's otherwise, so a note marked for scoring but not yet typed
    still appears in a per-type row rather than vanishing from the breakdown.
    """
    outcome = NoteOutcome(
        row_id=row.id,
        language=language_of(row.text),
        note_type=(
            str(row.note_type)
            if row.note_type is not None
            else (run.note_type or _UNKNOWN_TYPE)
        ),
    )

    def record(figure: str, expected: object, actual: object) -> None:
        matched = expected == actual
        outcome.figures[figure] = (int(matched), 1)
        if not matched:
            outcome.disagreements.append((figure, _show(expected), _show(actual)))

    if row.note_type is None:
        outcome.figures["classify"] = (0, 0)
    else:
        record("classify", str(row.note_type), run.note_type)

    if row.is_vague is None:
        outcome.figures["is_vague"] = (0, 0)
    else:
        record("is_vague", row.is_vague, run.is_vague)

    if row.missing_components is None:
        outcome.figures["components"] = (0, 0)
    else:
        expected_set = frozenset(c.value for c in row.missing_components)
        actual_set = (
            None
            if run.missing_components is None
            else frozenset(run.missing_components)
        )
        record("components", expected_set, actual_set)

    outcome.figures["checks"] = _compare_checks(row, run, outcome)

    expected_band = _expected_band(row, config)
    if expected_band is None:
        outcome.figures["band"] = (0, 0)
        # Only a note that WAS marked for scoring can be a marking error; one
        # with no checks at all is simply unmarked.
        outcome.band_not_computable = row.checks is not None
    else:
        record(
            "band", expected_band, None if run.score is None else run.score.band.value
        )

    return outcome


def _compare_checks(
    row: EvalRow, run: PassRun, outcome: NoteOutcome
) -> tuple[int, int]:
    """Agreement over the CHECK ANSWERS a human marked, one row per difference.

    Counted per answer rather than per note, because a note that got eight of
    nine right and a note that got none are not the same result -- and the
    prompt fix for one is not the fix for the other.

    A check the human marked and the model was never ASKED counts as a
    disagreement, not an exclusion: being asked the wrong checks is what a
    wrong classification does to a note, and it is exactly what this figure is
    meant to expose.
    """
    if row.checks is None:
        return (0, 0)
    matched = 0
    for check, expected in row.checks.items():
        actual = None if run.checks is None else run.checks.get(check)
        if actual == expected:
            matched += 1
        else:
            outcome.disagreements.append(
                (f"check.{check.value}", _show(expected), _show(actual))
            )
    return (matched, len(row.checks))


def _show(value: object) -> str:
    """A value as the CSV and the terminal show it: a fixed vocabulary or "-".

    None is "-" and never blank, so "the pass did not answer" reads differently
    from an empty cell somebody's spreadsheet ate.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, frozenset):
        return " ".join(sorted(value)) or "(none)"
    return str(value)


@dataclass(slots=True)
class PassStats:
    """One pass's cost and latency across the whole run."""

    name: str
    notes: int = 0
    reprompted: int = 0
    latencies: list[int] = field(default_factory=list)

    @property
    def reprompt_rate(self) -> float | None:
        return None if self.notes == 0 else 100.0 * self.reprompted / self.notes


@dataclass(slots=True)
class Summary:
    """Every figure this run produced, ready to print or assert on."""

    overall: dict[str, Tally]
    by_type: dict[str, dict[str, Tally]]
    by_language: dict[str, dict[str, Tally]]
    passes: list[PassStats]
    outcomes: list[NoteOutcome]
    notes: int
    failed: int
    tokens: int
    band_not_computable: int


def _empty_tallies() -> dict[str, Tally]:
    return {figure: Tally() for figure in _FIGURES}


def summarise(pairs: list[tuple[EvalRow, PassRun]], config: TenantConfig) -> Summary:
    """Every note's outcome, gathered into overall, per-type and per-language."""
    overall = _empty_tallies()
    by_type: dict[str, dict[str, Tally]] = {}
    by_language: dict[str, dict[str, Tally]] = {}
    passes = [PassStats(name=name) for name, _ in _PASSES]
    outcomes: list[NoteOutcome] = []

    for row, run in pairs:
        outcome = compare(row, run, config)
        outcomes.append(outcome)
        type_tallies = by_type.setdefault(outcome.note_type, _empty_tallies())
        language_tallies = by_language.setdefault(outcome.language, _empty_tallies())
        for figure, (matched, counted) in outcome.figures.items():
            for group in (overall, type_tallies, language_tallies):
                group[figure].add(matched, counted)

        for stats, (_, profile) in zip(passes, _PASSES, strict=True):
            calls = run.calls_for(profile)
            if calls == 0:
                continue
            stats.notes += 1
            # Two calls for one pass is the single reprompt and nothing else:
            # call_model sends once more on malformed output and then stops.
            stats.reprompted += int(calls > 1)
        for stats, latency in zip(
            passes, (run.classify_ms, run.vague_ms, run.score_ms), strict=True
        ):
            if latency is not None:
                stats.latencies.append(latency)

    return Summary(
        overall=overall,
        by_type=by_type,
        by_language=by_language,
        passes=passes,
        outcomes=outcomes,
        notes=len(pairs),
        failed=sum(1 for _, run in pairs if run.error),
        tokens=sum(run.total_tokens for _, run in pairs),
        band_not_computable=sum(1 for o in outcomes if o.band_not_computable),
    )


async def evaluate(
    client: LLMClient,
    rows: list[EvalRow],
    *,
    scope: TenantScope,
    config: TenantConfig,
    settings: Settings,
    recorder: RecordingClient | None = None,
    progress: bool = False,
) -> list[PassRun]:
    """Every row through the same `run_passes` the diagnostic uses.

    The SAME function, imported rather than reimplemented: a runner that built
    its own prompts would be reporting agreement for a pipeline the diagnostic
    cannot explain, and the first thing anyone does with a bad figure here is
    open the diagnostic CSV for that note.
    """
    runs: list[PassRun] = []
    for ordinal, row in enumerate(rows, start=1):
        run = await run_passes(
            client,
            row,
            ordinal,
            scope=scope,
            config=config,
            settings=settings,
            recorder=recorder,
        )
        runs.append(run)
        if progress:
            print(
                f"  {ordinal:>3}/{len(rows)}  {run.row_id:<16} "
                f"{run.gate or run.note_type or '-':<16} "
                f"{'' if run.score is None else run.score.band.value:<10} "
                f"{run.error or ''}"
            )
    return runs


def _percentile(values: list[int], percent: int) -> int | None:
    """Nearest-rank percentile, or None for an empty list.

    Nearest-rank rather than an interpolating one: every value here is a whole
    millisecond that actually happened, and a p95 of 812.5 is a number no call
    ever took. Integers in, an integer out.
    """
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percent / 100 * len(ordered)) - 1)
    return ordered[index]


def _cell(tally: Tally) -> str:
    """One figure as `matched/counted pct%`, or a dash when nothing counted."""
    if tally.share is None:
        return f"{'-':>13}"
    return f"{tally.matched:>4}/{tally.counted:<4}{tally.share:>4.0f}%"


def _report(summary: Summary) -> None:
    """The whole report, with the band figure LAST and on its own line."""
    header = "".join(f"{figure:>14}" for figure in _FIGURES[:-1])
    print("\nPASS AGREEMENT  (a note unmarked for a pass leaves that figure)")
    print(f"{'group':<22}{header}")
    print(
        f"{'overall':<22}" + "".join(_cell(summary.overall[f]) for f in _FIGURES[:-1])
    )

    print("-- by note type")
    for name in sorted(summary.by_type):
        tallies = summary.by_type[name]
        print(f"  {name:<20}" + "".join(_cell(tallies[f]) for f in _FIGURES[:-1]))

    print("-- by language")
    for name in sorted(summary.by_language):
        tallies = summary.by_language[name]
        print(f"  {name:<20}" + "".join(_cell(tallies[f]) for f in _FIGURES[:-1]))

    print("\nPER PASS")
    for stats in summary.passes:
        rate = stats.reprompt_rate
        print(
            f"  {stats.name:<10} notes {stats.notes:<4} "
            f"reprompts {stats.reprompted:<3} "
            f"({'-' if rate is None else f'{rate:.0f}%'})  "
            f"p50 {_ms(statistics.median(stats.latencies) if stats.latencies else None)}  "
            f"p95 {_ms(_percentile(stats.latencies, 95))}"
        )
    print(f"  tokens {summary.tokens}   failed notes {summary.failed}")
    if summary.band_not_computable:
        print(
            f"  {summary.band_not_computable} scored row(s) excluded from the band "
            "figure: the marked checks do not match the marked type's applicable "
            "set. Fix the marks."
        )

    # LAST, and on its own line. It is the business's acceptance target, and
    # everything above is here to explain a number that moved.
    band = summary.overall["band"]
    if band.share is None:
        print("\nBAND AGREEMENT: - (no note is marked for both a type and its checks)")
    else:
        print(
            f"\nBAND AGREEMENT: {band.share:.1f}%  "
            f"({band.matched} of {band.counted} marked notes; "
            f"target {BAND_TARGET_PERCENT}%)"
        )


def _ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f}ms"


def disagreement_rows(summary: Summary) -> list[dict[str, object]]:
    """One CSV row per disagreement, so the next prompt fix has a list to aim at."""
    return [
        {
            "id": outcome.row_id,
            "language": outcome.language,
            "note_type": outcome.note_type,
            "figure": figure,
            "expected": expected,
            "actual": actual,
        }
        for outcome in summary.outcomes
        for figure, expected, actual in outcome.disagreements
    ]


async def _run_live(
    rows: list[EvalRow], *, tenant: str, config: TenantConfig, settings: Settings
) -> list[PassRun]:
    scope = build_scope(tenant)
    async with httpx.AsyncClient(
        timeout=settings.external_call_timeout_seconds
    ) as http:
        recorder = RecordingClient(build_llm_client(settings, http))
        return await evaluate(
            recorder,
            rows,
            scope=scope,
            config=config,
            settings=settings,
            recorder=recorder,
            progress=True,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a scored set and report agreement with the hand marks."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "REQUIRED to place real, paid model calls. Without this flag the "
            "prompts are assembled and printed and nothing is sent."
        ),
    )
    parser.add_argument(
        "--tenant",
        default=DEFAULT_TENANT,
        help=f"Whose rubric to score against (default: {DEFAULT_TENANT}).",
    )
    parser.add_argument(
        "--assume-type",
        default=DEFAULT_ASSUMED_TYPE.value,
        choices=[t.value for t in NoteType if t is not NoteType.SYSTEM_EVENT],
        help="Dry run only: the type to assemble the vague and score prompts for.",
    )
    return parser.parse_args()


def main() -> int:
    use_utf8_output()
    args = _parse_args()
    path, rows = load_rows_or_exit()
    settings = get_settings()
    config = get_tenant_config(args.tenant)

    mode = "LIVE (real paid calls)" if args.live else "DRY RUN (nothing is sent)"
    print(f"Mode:       {mode}")
    print(f"Scored set: {path}  ({len(rows)} notes)")
    print(f"Tenant:     {args.tenant}  (config_version {config.config_version})")

    if not args.live:
        print_prompts(rows, config, NoteType(args.assume_type))
        print("\n(DRY RUN: no call was placed. Pass --live to run the eval.)")
        return 0

    # 139's two guards, imported rather than rewritten: a second copy of a
    # refusal is a second thing to get wrong, and the one that is wrong is the
    # one that spends the money.
    _guard_against_accidental_live_call(args.live)
    _refuse_an_oversized_run(rows)

    runs = asyncio.run(
        _run_live(rows, tenant=args.tenant, config=config, settings=settings)
    )
    summary = summarise(list(zip(rows, runs, strict=True)), config)
    _report(summary)

    rows_out = disagreement_rows(summary)
    if rows_out:
        destination: Path = output_path(path, "disagreements")
        write_csv(destination, rows_out)
        print(f"\n{len(rows_out)} disagreement(s) written to {destination}")
    else:
        print("\nNo disagreements to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
