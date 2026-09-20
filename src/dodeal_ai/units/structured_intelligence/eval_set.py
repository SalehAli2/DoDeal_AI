"""The scored set — the format a human marks notes in, and the loader for it.

REAL NOTES NEVER ENTER THIS REPO, and this module is the mechanism rather than
the promise. The path is given at runtime through an environment variable, it
is resolved, and a path that lands anywhere inside the repository is REFUSED
before the file is opened. That is why the variable is not a `Settings` field:
a field would appear in `.env.example`, be copied into a deployment, and turn a
hand-marking tool into something the service could be configured to read. It
works the way `DODEAL_REDIS_REAL_URL` works -- lane configuration, read by the
things that run the lane and by nothing the service starts.

ONE JSON OBJECT PER LINE, and the line number is the handle. A malformed row is
refused naming the LINE, never the text: the text is a real salesperson's note
by the time this matters, and an error message is the easiest place in a
program for one to escape to a terminal, a CI log or a pasted ticket.

EVERY EXPECTED FIELD IS OPTIONAL, AND ABSENT IS NOT A DEFAULT. A set is marked
by a person over days; on any morning most rows are marked for some passes and
not others. `None` means "no human has answered this yet" and travels as None
all the way to the runner, which EXCLUDES it rather than scoring the pipeline
against a value nobody chose. A loader that defaulted `is_vague` to False would
manufacture agreement out of unfinished work -- and the number it manufactured
is the number the business is being asked to accept.

THERE IS NO `band` FIELD AND NO `total` FIELD, and `extra="forbid"` is what
stops one arriving. A band is computed from check answers, the note type and
the tenant's rubric, in code (scoring.compute_score); a scored set that carried
its own band would be a second source for the one number the whole unit exists
to justify. The human marks the twelve facts; the arithmetic stays where the
arithmetic is.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from dodeal_ai.schemas.lead import Lead, LeadNote
from dodeal_ai.units.structured_intelligence.schemas import (
    CheckName,
    ClassifierOutput,
    LeadContext,
    MissingComponent,
)

# Where the scored set lives, given at runtime. An ENVIRONMENT VARIABLE and not
# a Settings field, so it can never be deployed with the service (see the module
# docstring). Unset is the default and means every eval test skips; a wrong
# value is a refusal naming the path, never a silent empty run.
EVAL_SET_PATH_ENV = "DODEAL_EVAL_SET_PATH"

# The repository, as this file can see it: src/dodeal_ai/units/structured_intelligence
# is four levels down. Nothing under here may be a scored set, because a scored
# set holds real notes. A wrong value here would let a real note be committed,
# so a test asserts this really is the repository root.
REPO_ROOT = Path(__file__).resolve().parents[4]

# The timestamp every synthesised note carries. LeadNote requires one and
# nothing in the three passes reads it, so a fixed, obviously-not-real value is
# used rather than the clock. A wrong value changes nothing; a value from the
# clock would make two runs of the same set differ in their inputs.
_FIXED_CREATED_AT = datetime(2000, 1, 1, tzinfo=UTC)

# The author id every synthesised note carries. The three passes never read it
# -- it reaches the judgement and the outcome line, neither of which this
# harness builds. A wrong value changes nothing, which is why it is fixed.
_FIXED_AUTHOR_ID = 1


class EvalSetError(Exception):
    """The scored set could not be read, or a row in it is not a row.

    Carries a path, a line number and pydantic's own field locations and error
    types. It NEVER carries a field value, so nothing a note said can reach the
    terminal through a failure -- which is the one place an error message is
    read aloud, pasted and kept.
    """


class EvalRow(BaseModel):
    """One hand-marked note: what it is, and what a human says the answer is.

    `id`, `text` and `lead` describe the note and are required -- a row without
    them is not a row. The four expected fields below are each optional and
    default to None, which means UNMARKED and never "no".

    `extra="forbid"` is load-bearing twice over. It refuses a `band` or a
    `total` (see the module docstring), and it refuses a typo: `is_vauge` in a
    file somebody typed by hand would otherwise read as an unmarked note and
    quietly shrink the vague figure's denominator.
    """

    model_config = ConfigDict(extra="forbid")

    # The handle a diagnostic row and a disagreement row are read by. A string
    # rather than an int so a marker can use "ar-03" or "batch2-17"; the note id
    # the passes need is synthesised from the row's position (as_inputs below).
    id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    # The four lead fields the classifier is allowed to see, and exactly those:
    # the same model the direct route's body carries, so a scored set cannot
    # widen what reaches a prompt either.
    lead: LeadContext

    # --- the expected answers. None everywhere means "not marked by hand yet".
    # `unclassifiable` is a legal mark: it is an answer the classifier can give.
    note_type: ClassifierOutput | None = None
    # StrictBool, as on ScoreOutput: pydantic's lax mode reads "yes" as True,
    # and a hand-typed file is exactly where "yes" gets written. A mark nobody
    # meant is worse than a refusal somebody has to fix.
    is_vague: StrictBool | None = None
    # An empty list is a MARK ("nothing is missing"), not an absence. Only the
    # field being absent means unmarked, which is why this is `| None` and not
    # a list defaulting to empty.
    missing_components: list[MissingComponent] | None = None
    # A subset is allowed and expected: which checks apply depends on the note
    # type and the tenant's rubric (scoring.applicable_checks), so a no_contact
    # note has fewer than twelve to answer. Empty is refused -- it would be
    # indistinguishable from unmarked. StrictBool, as on ScoreOutput, so the
    # string "true" is a refusal rather than a mark.
    checks: dict[CheckName, StrictBool] | None = Field(default=None, min_length=1)


def configured_path() -> Path | None:
    """The scored set's path from the environment, or None when it is unset.

    None is the ordinary case and is not an error: every eval test skips on it,
    and CI never sets it. Blank is treated as unset -- an exported-but-empty
    variable is how a shell says "I did not set this".
    """
    raw = os.environ.get(EVAL_SET_PATH_ENV, "").strip()
    return Path(raw) if raw else None


def load_eval_set(path: Path) -> list[EvalRow]:
    """Every row in `path`, validated, in file order.

    Refuses, in this order: a path inside the repository, a file that cannot be
    read, a line that is not JSON, a row that is not a row, a repeated id, and a
    file with no rows at all. Blank lines are skipped, because a file somebody
    edits by hand ends with one.

    The id check is here rather than in the runner because a repeated id makes
    every row that carries it unidentifiable in a diagnostics CSV -- which is
    the one thing the CSV is for.
    """
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(REPO_ROOT):
        raise EvalSetError(
            f"refusing a scored set inside the repository: {resolved}. "
            "Real notes never enter this repo -- keep the set outside it."
        )

    try:
        raw = resolved.read_text(encoding="utf-8")
    except OSError as exc:
        # The exception TYPE, never its message: an OSError's message is the
        # operating system's and is not worth the risk of finding out what it
        # decided to include.
        raise EvalSetError(f"cannot read {resolved}: {type(exc).__name__}") from None

    rows: list[EvalRow] = []
    seen: set[str] = set()
    for number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except ValueError:
            # from None, and no decoder message: a JSONDecodeError carries the
            # offending document on `.doc`.
            raise EvalSetError(
                f"{resolved.name} line {number}: not one JSON object"
            ) from None
        try:
            row = EvalRow.model_validate(payload)
        except ValidationError as exc:
            raise EvalSetError(
                f"{resolved.name} line {number}: {_problems(exc)}"
            ) from None
        if row.id in seen:
            raise EvalSetError(f"{resolved.name} line {number}: repeated id")
        seen.add(row.id)
        rows.append(row)

    if not rows:
        raise EvalSetError(f"{resolved.name}: no rows")
    return rows


def _problems(exc: ValidationError) -> str:
    """Pydantic's field locations and error types, and nothing it was given.

    `include_input=False` is what keeps the rejected value out, exactly as in
    core/validation.py. The two are the same rule applied to two untrusted
    sources: a model's answer there, a real note here.
    """
    return ", ".join(
        f"{'.'.join(str(part) for part in item['loc']) or '(root)'}: {item['type']}"
        for item in exc.errors(
            include_url=False, include_input=False, include_context=False
        )
    )


def as_inputs(row: EvalRow, ordinal: int) -> tuple[Lead, LeadNote]:
    """The `Lead` and `LeadNote` the three passes take, built from one row.

    Here rather than in either script so the diagnostic and the runner send
    BYTE-IDENTICAL prompts for the same row: two scripts building their own
    notes is how a diagnosis stops describing the run it was meant to explain.

    `ordinal` is the row's position, used for both ids. They are synthetic and
    never leave this process -- no backend is called, nothing is reserved, and
    no counter is keyed on them.
    """
    lead = Lead(id=ordinal, **row.lead.model_dump())
    note = LeadNote(
        id=ordinal,
        note=row.text,
        author=None,
        author_id=_FIXED_AUTHOR_ID,
        createdAt=_FIXED_CREATED_AT,
    )
    return lead, note
