"""Diagnostic harness — one note in, one row out, per pass.

WHY A ROW AND NOT A JUDGEMENT. A judgement is one number, and a wrong number
says nothing about WHICH of the three passes produced it. The passes are a
chain: the classifier's answer chooses the vague template AND the applicable
components, so a note classified `discovery` that was really `no_contact` is
asked the wrong question by pass two and scored against the wrong denominator
by pass three. All of that arrives as a single 200 with a band on it. This
writes what each pass actually said, so a bad band can be traced to the pass
that caused it instead of being argued about.

IT IS NOT THE PIPELINE, AND DOES NOT PRETEND TO BE. It runs the length gate and
the three passes -- the parts that decide what the AI says -- and it runs
NOTHING that decides what the SERVICE does: no idempotency reservation, no
attempt counter, no rate limit, no token pre-flight, no decision. Those are
production concerns with their own tests; putting them here would mean a
diagnostic run could 409 or be rate limited, which tells nobody anything about
the model. The one exception is the token CHARGE inside `llm_call.complete_once`,
which cannot be reached around without patching a factory this repo forbids
scripts to patch -- it fails open, and it is inside the per-pass milliseconds
exactly as it is inside pipeline.py's.

REAL PAID CALLS, SO --live IS REQUIRED, and the guard has two independent
halves, like scripts/real_fetch_check.py: the dry run takes a different branch
that never builds a provider client, AND `guard_against_accidental_live_call`
refuses immediately before the client is built even if that branch is wrong.
Without --live, the three prompts are assembled and printed and nothing is
sent.

THE COST GUARD. --live refuses more than MAX_LIVE_NOTES notes in one run and
prints the call count it is about to spend before it spends any of it. It never
retries and never loops: a note whose pass raises is ONE row with the failure
type recorded, and the run carries on to the next note. A harness that retried
would turn a provider having a bad day into an unbounded bill from a script
nobody is watching.

NOT A TEST. No `test_` name, no coverage floor, never in CI. Its guard is a
hand run, which is the only thing that proves a real provider answers in a
shape these columns can hold.

Usage:
    uv run python -m scripts.diagnose_notes             # dry run, sends nothing
    uv run python -m scripts.diagnose_notes --live      # real paid calls

Requires:
    - DODEAL_EVAL_SET_PATH pointing at a scored set OUTSIDE this repository
      (src/dodeal_ai/units/structured_intelligence/eval_set.py refuses one
      inside it). The CSV is written beside that file.
    - DODEAL_LLM_* configured, for --live only.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import sys
import time
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx

from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import TenantScope
from dodeal_ai.core.llm import LLMClient, LLMResponse, build_llm_client
from dodeal_ai.core.llm.profiles import (
    PROFILE_UNIT_A_CLASSIFY,
    PROFILE_UNIT_A_SCORE,
    PROFILE_UNIT_A_VAGUE,
)
from dodeal_ai.core.prompting import AssembledPrompt, PromptError
from dodeal_ai.core.redaction import redact
from dodeal_ai.core.resilience import gather_or_cancel
from dodeal_ai.schemas.lead import Lead, LeadNote
from dodeal_ai.units.structured_intelligence.classify import (
    build_classification_prompt,
    classify,
    suppression_for,
)
from dodeal_ai.units.structured_intelligence.config import (
    TenantConfig,
    get_tenant_config,
)
from dodeal_ai.units.structured_intelligence.eval_set import (
    EVAL_SET_PATH_ENV,
    EvalRow,
    EvalSetError,
    as_inputs,
    configured_path,
    load_eval_set,
)
from dodeal_ai.units.structured_intelligence.pipeline import (
    _length_gate,
    _recognised_short,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    CheckName,
    ComponentName,
    NoteScore,
    NoteType,
)
from dodeal_ai.units.structured_intelligence.scoring import (
    build_score_prompt,
    compute_score,
    score_note,
)
from dodeal_ai.units.structured_intelligence.vague import (
    build_vague_prompt,
    detect_vagueness,
)

# The most notes one --live run will spend on. Twenty notes is sixty calls, plus
# whatever reprompts: enough to diagnose a prompt change, small enough that a
# mistyped path cannot empty a budget. Over it the run STOPS and says to split
# the file -- it never silently truncates, because a run that scored the first
# twenty of a hundred and said so quietly is a result somebody will quote.
MAX_LIVE_NOTES = 20

# Three passes per note, when every pass runs. A reprompt makes it more, which
# is why the printed figure says "at least": a malformed answer buys exactly one
# more call per pass (llm_call.call_model), so the true ceiling is twice this.
CALLS_PER_NOTE = 3

# The tenant whose rubric is used when none is named. It selects the weights,
# the bands and the suppressed components -- so a diagnostic run under the wrong
# tenant reports marks nobody's rubric produces. Printed in the header for that
# reason.
DEFAULT_TENANT = "tenant-a"

# The tenant every eval run's tokens are CHARGED to (register item 140), whose
# rubric is never read: --tenant chooses the rubric, this keeps a diagnostic run
# out of a real tenant's token budget. Printed beside the rubric's tenant.
EVAL_SCRATCH_TENANT = "eval-scratch"

# The note type the DRY RUN assembles the vague and score prompts for. A real
# run learns the type from pass one; a dry run has not called anything, so it
# has to assume one and say so. Discovery is the type with the most applicable
# components, so its prompts are the longest -- the useful ones to eyeball.
DEFAULT_ASSUMED_TYPE = NoteType.DISCOVERY


@dataclass(slots=True)
class Call:
    """One call placed at the provider: which task, and what it cost."""

    profile: str
    input_tokens: int = 0
    output_tokens: int = 0


class RecordingClient:
    """An LLMClient that counts the calls placed through it, per task.

    A SECOND CALL FOR ONE PASS IS A REPROMPT -- that is the only way one pass
    places two (`llm_call.call_model` sends once more on malformed output and
    then stops). Counting here rather than reading a log line is what makes the
    reprompt rate a number this harness can put in a column: the log says a
    reprompt was issued, but nothing joins that line to the note it was for.

    The call is recorded BEFORE it is awaited, so a call that raises is still
    counted. It was placed, and it may well have been paid for.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.calls: list[Call] = []

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        profile: str,
        max_output_tokens: int | None = None,
    ) -> LLMResponse:
        record = Call(profile=profile)
        self.calls.append(record)
        response = await self._inner.complete(
            prompt, profile=profile, max_output_tokens=max_output_tokens
        )
        record.input_tokens = response.input_tokens
        record.output_tokens = response.output_tokens
        return response

    def take(self) -> list[Call]:
        """The calls since the last take, and reset. One note's worth."""
        taken = self.calls
        self.calls = []
        return taken


@dataclass(slots=True)
class PassRun:
    """Everything the three passes said about one note.

    Every field is None where the pass DID NOT RUN, never a zero and never a
    default: a note the length gate suppressed did not take zero milliseconds
    to classify and was not classified `no_contact`. The same rule as
    pipeline.py's `_Timings`, for the same reason -- a made-up zero averages
    into a figure and drags it somewhere nobody chose.
    """

    row_id: str
    gate: str | None = None
    recognised_short: bool = False
    note_type: str | None = None
    is_vague: bool | None = None
    missing_components: tuple[str, ...] | None = None
    clarification_prompt: str | None = None
    checks: dict[CheckName, bool] | None = None
    score: NoteScore | None = None
    classify_ms: int | None = None
    vague_ms: int | None = None
    score_ms: int | None = None
    calls: list[Call] = field(default_factory=list)
    # The exception TYPE, never its message: a provider's exception message is
    # foreign text and may quote the note back (core/log_safety.py's rule).
    error: str | None = None

    def calls_for(self, profile: str) -> int:
        return sum(1 for call in self.calls if call.profile == profile)

    @property
    def total_tokens(self) -> int:
        return sum(call.input_tokens + call.output_tokens for call in self.calls)


async def _timed[T](coro: Awaitable[T]) -> tuple[T, int]:
    """Await `coro` and say how long it took, in whole milliseconds.

    A clock EACH, exactly as in pipeline.py: the vague and score passes run at
    the same time, so one pair of readings around the gather would measure the
    slower of them twice. `time.monotonic` because it is the one clock that
    cannot run backwards.
    """
    started = time.monotonic()
    result = await coro
    return result, int((time.monotonic() - started) * 1000)


async def run_passes(
    client: LLMClient,
    row: EvalRow,
    ordinal: int,
    *,
    scope: TenantScope,
    config: TenantConfig,
    settings: Settings,
    recorder: RecordingClient | None = None,
) -> PassRun:
    """One note through the length gate and the three passes, in pipeline order.

    THE ORDER IS NOT A STYLE CHOICE. The gate runs first because a suppressed
    note must cost nothing; classification runs alone because its answer picks
    the vague template and the applicable checks; vague and score are gathered
    because they are independent of each other and nothing else waits on them.
    A harness that ran them in a different order would be diagnosing a pipeline
    that does not exist.

    EVERY FAILURE IS ONE ROW, AND THE RUN CONTINUES. There is no retry here and
    no loop: a note whose pass raises records the exception type and returns.
    `BaseException` is not caught -- a cancellation or a KeyboardInterrupt must
    still stop the run rather than become a row.
    """
    run = PassRun(row_id=row.id)
    lead, note = as_inputs(row, ordinal)
    # Register item 132: the recognition test runs only for a note BELOW the
    # floor, which is what `_recognised_short` is -- pipeline.py's own answer,
    # imported rather than rebuilt here. Asked of every note, as it was, a full
    # note opening with a tenant phrase read back as a recognised short one and
    # the column said the table was carrying notes it had never seen.
    run.recognised_short = _recognised_short(note, config)

    gated = _length_gate(note, config)
    if gated is not None:
        _, detail = gated
        run.gate = detail.value
        return run

    # The redacted copy is what the three prompts read (register item 59), and
    # the ORIGINAL is what the gate counted -- the same split as pipeline.py.
    prompt_note = note.model_copy(update={"note": redact(note.note).text})

    try:
        await _fill(
            run,
            client,
            prompt_note,
            lead,
            scope=scope,
            config=config,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001 - one failed note is one row
        run.error = type(exc).__name__
    finally:
        if recorder is not None:
            run.calls = recorder.take()
    return run


async def _fill(
    run: PassRun,
    client: LLMClient,
    note: LeadNote,
    lead: Lead,
    *,
    scope: TenantScope,
    config: TenantConfig,
    settings: Settings,
) -> None:
    """The three passes themselves, filling `run` as each one answers.

    Separate from `run_passes` so the failure handler there wraps every pass
    without an indentation level per pass -- and so a pass that fails leaves
    the passes BEFORE it recorded, which is most of the diagnosis.
    """
    (classification, _), run.classify_ms = await _timed(
        classify(client, note, lead, scope=scope, settings=settings)
    )
    run.note_type = str(classification.note_type)

    if suppression_for(classification.note_type) is not None:
        # system_event or unclassifiable: the pipeline stops here and so does
        # this. Two more passes would be two more paid calls for an answer the
        # service would never publish.
        return

    note_type = classification.note_type
    assert isinstance(note_type, NoteType)

    (vague_result, run.vague_ms), (score_result, run.score_ms) = await gather_or_cancel(
        _timed(
            detect_vagueness(
                client, note, note_type, scope=scope, config=config, settings=settings
            )
        ),
        _timed(
            score_note(
                client, note, note_type, scope=scope, config=config, settings=settings
            )
        ),
    )
    vague_output, _ = vague_result
    score_output, _ = score_result

    run.is_vague = vague_output.is_vague
    run.missing_components = tuple(c.value for c in vague_output.missing_components)
    run.clarification_prompt = vague_output.clarification_prompt
    run.checks = dict(score_output.checks)
    # The band is COMPUTED, here as everywhere: from the check answers, the
    # type and the tenant's rubric. Nothing the model said is a band.
    run.score = compute_score(score_output.checks, note_type, config)


def _row_for(run: PassRun) -> dict[str, object]:
    """One PassRun as one CSV row, in the column order below."""
    marks: dict[ComponentName, int | None] = {}
    if run.score is not None:
        marks = {c.name: c.mark for c in run.score.components}

    row: dict[str, object] = {
        "id": run.row_id,
        "gate": run.gate or "",
        "recognised_short": _bool(run.recognised_short),
        "note_type": run.note_type or "",
        "is_vague": _bool(run.is_vague),
        "missing_components": " ".join(run.missing_components or ()),
        "clarification_prompt": run.clarification_prompt or "",
    }
    for check in CheckName:
        answered = None if run.checks is None else run.checks.get(check)
        row[f"check.{check.value}"] = _bool(answered)
    for component in ComponentName:
        mark = marks.get(component)
        row[f"mark.{component.value}"] = "" if mark is None else mark
    row["denominator"] = "" if run.score is None else run.score.denominator
    row["total"] = "" if run.score is None else run.score.total
    row["band"] = "" if run.score is None else run.score.band.value
    row["classify_ms"] = "" if run.classify_ms is None else run.classify_ms
    row["vague_ms"] = "" if run.vague_ms is None else run.vague_ms
    row["score_ms"] = "" if run.score_ms is None else run.score_ms
    row["classify_calls"] = run.calls_for(PROFILE_UNIT_A_CLASSIFY)
    row["vague_calls"] = run.calls_for(PROFILE_UNIT_A_VAGUE)
    row["score_calls"] = run.calls_for(PROFILE_UNIT_A_SCORE)
    row["tokens"] = run.total_tokens
    row["error"] = run.error or ""
    return row


def _bool(value: bool | None) -> str:
    """true / false / empty. Empty is "the pass did not answer", not false."""
    return "" if value is None else ("true" if value else "false")


def output_path(source: Path, kind: str) -> Path:
    """Where a CSV goes: BESIDE the input, never in the repo.

    The input path was already refused if it resolved inside the repository
    (eval_set.load_eval_set), so a sibling of it is outside by construction --
    there is no second guard here because there is no second decision.

    Stamped with the time so a second run does not silently overwrite the run
    somebody is comparing it against. That is the whole use of this file: two
    runs either side of a prompt change.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return source.with_name(f"{source.stem}.{kind}.{stamp}.csv")


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    """Write `rows` with the first row's keys as the header.

    newline="" is required on Windows: csv writes its own \\r\\n, and without
    this the file gets \\r\\r\\n and every second line reads blank in a
    spreadsheet.
    """
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def build_scope(tenant: str = EVAL_SCRATCH_TENANT) -> TenantScope:
    """The minimal scope the three passes read: a tenant and a subject. The
    tenant is who the tokens are charged to -- eval-scratch by default
    (register item 140), never the tenant whose rubric scores the run.

    A TenantScope built directly, as scripts/real_fetch_check.py builds one:
    there is no gate chain here to narrow a context from, which is the
    non-user-principal case design note 0001 D1 describes. The subject reaches
    the token charge and nothing else.
    """
    return TenantScope(
        tenant=tenant,
        subject="diagnose-notes",
        database="",
        request_id="diagnose-notes",
    )


def guard_against_accidental_live_call(live: bool) -> None:
    """Defense in depth: refuse to build a provider client without --live.

    The dry run already takes a branch that never reaches here. This is the
    second half, and it is the half that holds when the first is wrong: a paid
    call to a real provider must be a deliberate act, never a side effect of
    an edit to a branch condition.
    """
    if not live:
        print(
            "REFUSING: this would build a real provider client and place paid "
            "calls, and --live was not passed. Pass --live to confirm you "
            "intend to spend money.",
            file=sys.stderr,
        )
        raise SystemExit(3)


def refuse_an_oversized_run(rows: list[EvalRow]) -> None:
    """The cost guard: how much this is about to spend, and a ceiling on it."""
    if len(rows) > MAX_LIVE_NOTES:
        print(
            f"REFUSING: {len(rows)} notes is over the {MAX_LIVE_NOTES}-note "
            f"limit for one --live run (at least {len(rows) * CALLS_PER_NOTE} "
            "paid calls). Split the file and run it in parts.",
            file=sys.stderr,
        )
        raise SystemExit(4)
    print(
        f"About to spend at least {len(rows) * CALLS_PER_NOTE} paid calls "
        f"({len(rows)} notes x {CALLS_PER_NOTE} passes; a reprompt makes it "
        "more). No call is ever retried."
    )


def print_prompts(rows: list[EvalRow], config: TenantConfig, assumed: NoteType) -> None:
    """The dry run: assemble what would be sent, and send nothing.

    The vague and score prompts depend on the CLASSIFIER'S ANSWER, which a dry
    run has not got -- so they are assembled for `assumed` and labelled as
    assumed. Printing them for a type the run did not choose and not saying so
    would be worse than not printing them.

    This prints the caller-data section, which a log line never may. A terminal
    a person is reading is not a log line, and the file being read is their own
    scored set -- but nothing here writes any of it to a file.
    """
    for ordinal, row in enumerate(rows, start=1):
        lead, note = as_inputs(row, ordinal)
        prompt_note = note.model_copy(update={"note": redact(note.note).text})
        print(
            f"\n{'=' * 70}\n=== {row.id}  (note {ordinal} of {len(rows)})\n{'=' * 70}"
        )
        gated = _length_gate(note, config)
        if gated is not None:
            print(
                f"--- SUPPRESSED by the length gate: {gated[1].value}; no prompt is sent"
            )
            continue
        print(
            f"\n--- classify ---\n{build_classification_prompt(prompt_note, lead).text}"
        )
        print(f"\n--- vague (ASSUMING note_type={assumed.value}) ---")
        print(build_vague_prompt(prompt_note, assumed).text)
        print(f"\n--- score (ASSUMING note_type={assumed.value}) ---")
        print(build_score_prompt(prompt_note, assumed, config).text)


async def _run_live(
    rows: list[EvalRow], *, config: TenantConfig, settings: Settings
) -> list[PassRun]:
    """Every note through the three passes, against the real provider."""
    scope = build_scope()  # charged to eval-scratch; `config` is --tenant's
    runs: list[PassRun] = []
    async with httpx.AsyncClient(
        timeout=settings.external_call_timeout_seconds
    ) as http:
        recorder = RecordingClient(build_llm_client(settings, http))
        for ordinal, row in enumerate(rows, start=1):
            run = await run_passes(
                recorder,
                row,
                ordinal,
                scope=scope,
                config=config,
                settings=settings,
                recorder=recorder,
            )
            runs.append(run)
            print(
                f"  {ordinal:>3}/{len(rows)}  {run.row_id:<16} "
                f"{run.gate or run.note_type or '-':<16} "
                f"{'' if run.score is None else run.score.band.value:<10} "
                f"{run.error or ''}"
            )
    return runs


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a scored set through the three Unit A passes, one CSV row per note."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "REQUIRED to place real, paid model calls. Without this flag the "
            "three prompts are assembled and printed and nothing is sent."
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


def use_utf8_output() -> None:
    """Make stdout and stderr carry Arabic, whatever the console's codepage.

    Most of this corpus is Arabic and so are the templates' worked examples. A
    Windows console defaults to cp1252, which cannot encode a single one of
    those characters -- so without this the dry run dies with a
    UnicodeEncodeError halfway through the first prompt, and a --live run dies
    printing the first Arabic clarification question. errors="replace" rather
    than "strict": a terminal that still cannot render a glyph should show a
    box, not end the run.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def load_rows_or_exit() -> tuple[Path, list[EvalRow]]:
    """The configured scored set, or a usage message and a non-zero exit."""
    path = configured_path()
    if path is None:
        print(
            f"Set {EVAL_SET_PATH_ENV} to a scored set OUTSIDE this repository.\n"
            "  One JSON object per line; see "
            "src/dodeal_ai/units/structured_intelligence/eval_set.py for the format.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    try:
        return path, load_eval_set(path)
    except EvalSetError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


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
    print(f"Charged to: {EVAL_SCRATCH_TENANT}  (tokens only; the rubric is --tenant's)")

    if not args.live:
        try:
            print_prompts(rows, config, NoteType(args.assume_type))
        except PromptError as exc:
            print(f"FAILED to assemble a prompt: {exc}", file=sys.stderr)
            return 1
        print("\n(DRY RUN: no call was placed. Pass --live to run the passes.)")
        return 0

    # The flag guard first, the cost guard second: a run that is not allowed to
    # spend should be refused before it is told how much it was going to spend.
    guard_against_accidental_live_call(args.live)
    refuse_an_oversized_run(rows)
    runs = asyncio.run(_run_live(rows, config=config, settings=settings))

    destination = output_path(path, "diagnostics")
    write_csv(destination, [_row_for(run) for run in runs])
    failed = sum(1 for run in runs if run.error)
    print(f"\nWrote {len(runs)} rows to {destination}")
    print(f"Failed notes: {failed}. Tokens: {sum(r.total_tokens for r in runs)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
