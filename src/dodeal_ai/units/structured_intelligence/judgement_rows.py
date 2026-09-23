"""JudgementRow — the row the CRM stores for every judgement, and the fake
that stands in for its store until the backend answers.

THIS SERVICE HOLDS NO HISTORY. It judges one note, answers, and forgets: the
only things it keeps are counters and reservations in Redis, with expiry. What
we said about a note is the CRM's to store. Tracking (register item 144) and
the briefs (item 145) both read those rows back, so the shape has to exist
before either can be written -- and it cannot be invented later, because by
then two readers depend on it.

THE FIELD NAMES BELOW ARE A CONTRACT. This model IS the attachment for the
backend ask: a backend engineer implements the store from it, field for field.
So they are chosen as a schema and not as an internal convenience -- snake_case
throughout, including `note_created_at`, even though the notes endpoint spells
its own timestamp `createdAt`. One style, picked once, is worth more to the
integration than matching whichever style the neighbouring endpoint happens to
use; and `created_at` alone would be read as the ROW's creation time, which is
a different instant and not one we ask them to store.

THERE IS NO NOTE TEXT AND THERE NEVER MAY BE ONE. A row that carried a note
body would make this service a second copy of the CRM's data -- a second place
to breach, a second place to honour a deletion, and a second thing to keep in
step with an edit. The exact field set is asserted by a test, so adding one
fails the suite rather than the review.

THE FAKE IS A FAKE, and is named one. `FileJudgementStore` reads INVENTED rows
from a JSON file whose path comes from an environment variable, exactly as the
scored set does (eval_set.py) and for the same two reasons: the variable is not
a `Settings` field, so it can never be deployed with the service, and a path
inside this repository is refused before the file is opened.

LOADED AT STARTUP (register item 155): the lifespan reads the file once, when
the variable is set, and a malformed or in-repo file refuses startup naming the
store and never the path. `get_judgement_store` hands out what was loaded, so
no request reads disk. A refusal here carries fixed text, a row's position and
an exception type: never a path, a file name, a tenant key or a value.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi import Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dodeal_ai.core.errors import BriefStoreUnavailable
from dodeal_ai.units.structured_intelligence.eval_set import REPO_ROOT
from dodeal_ai.units.structured_intelligence.schemas import (
    Band,
    ClassifierOutput,
    EnforcementVerdict,
    SuppressedReason,
)

# Where the invented rows live, given at runtime. An ENVIRONMENT VARIABLE and
# not a Settings field, so the fake can never be configured into a deployment:
# a `Settings` field would reach `.env.example` and be copied into production.
# Unset is the ordinary case and means there is no store.
JUDGEMENT_ROWS_PATH_ENV = "DODEAL_JUDGEMENT_ROWS_PATH"


class JudgementStoreError(Exception):
    """The rows could not be read, or a row in them is not a row.

    Carries the tenant key, the row's position and pydantic's field locations
    and error types. It NEVER carries a field value: a row holds a real lead's
    id and a real salesperson's id by the time this matters, and an error
    message is the easiest place in a program for one to reach a terminal.
    """


class JudgementRow(BaseModel):
    """One stored judgement, as the CRM holds it and as we ask them to.

    Exactly one of the two halves is filled, the same invariant `Judgement`
    itself carries: a SCORED row has band, total and denominator with
    `suppressed_reason` null; a SUPPRESSED row has `suppressed_reason` with all
    three null. A row with both, or with neither, is malformed and is refused
    here rather than averaged into a measure downstream.

    `extra="forbid"` is load-bearing twice. It refuses a note body arriving
    under any name at all, and it refuses a typo in a hand-written fixture that
    would otherwise read as an unset optional.

    Frozen, because a measure reads these and must not be able to change one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ge=1 on every id, the same rule as the request bodies (register item 10):
    # a 0 or a negative id is not a row, whoever wrote it.
    note_id: int = Field(ge=1)
    lead_id: int = Field(ge=1)
    author_id: int = Field(ge=1)

    # THE NOTE'S timestamp, not the judgement's. It is what the rolling window
    # in item 144 is measured over, because a rep's day is the day they did the
    # work -- not the day a backfill happened to judge it.
    note_created_at: datetime

    # Null when we stopped before classifying (the length gate); "unclassifiable"
    # when the classifier declined, which is an answer and not an absence.
    note_type: ClassifierOutput | None = None

    # --- the scored half -----------------------------------------------------
    # `total` is already rescaled to 0-100 by compute_score, so it does NOT sit
    # inside `denominator`: the denominator is the sum of the applicable weights
    # (80, or 35 for no_contact, today) and is carried because it says which
    # components could be answered at all.
    band: Band | None = None
    total: int | None = Field(default=None, ge=0, le=100)
    denominator: int | None = Field(default=None, ge=1, le=100)

    # --- the suppressed half -------------------------------------------------
    # The reason, not the detail: the detail names WHICH of our limits or the
    # note's it was, and no measure reads it. A field nobody reads is a field
    # the backend has to store correctly for nothing.
    suppressed_reason: SuppressedReason | None = None

    # Whether a clarification prompt actually reached the salesperson. Both
    # halves may carry it true: a note below the length floor is suppressed and
    # still asks a fixed question (register item 64).
    prompt_sent: bool

    # Required on every row, scored or suppressed, exactly as on the judgement
    # (register item 142). An absent verdict is not an allow.
    enforcement_verdict: EnforcementVerdict

    # The four stamps. Two rows are only comparable when all four match, which
    # is what lets a weight or a prompt change without rescoring history -- and
    # it is why the store must keep them rather than looking them up later.
    rubric_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    # The one stamp that may be empty: "" is pipeline.NO_MODEL and means no
    # model ran, which is the honest value on a judgement nothing was spent on.
    model_version: str
    config_version: str = Field(min_length=1)

    @field_validator("note_created_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        """Always aware. ASSUMPTION[Q5], the same rule LeadNote applies to the
        same instant: a timestamp with no offset is UTC. Two rules for one
        value would shift a rep's day by the offset and never fail."""
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    @model_validator(mode="after")
    def _exactly_one_half(self) -> JudgementRow:
        """Scored or suppressed, never both and never neither.

        Fixed-vocabulary messages: field names, never values. A row that failed
        this is a row whose ids we still must not print.
        """
        scored = (self.band, self.total, self.denominator)
        if self.suppressed_reason is None:
            if any(part is None for part in scored):
                raise ValueError(
                    "a row with no suppressed_reason needs band, total and denominator"
                )
        elif any(part is not None for part in scored):
            raise ValueError("a suppressed row carries no band, total or denominator")
        return self


@runtime_checkable
class JudgementStore(Protocol):
    """The one read the measures and the briefs need, and nothing else.

    ONE METHOD ON PURPOSE. Every caller in this unit wants the same thing: a
    tenant's judgements over a window, optionally narrowed to one salesperson.
    A store that also offered "the last N" or "this note's history" would be a
    store the backend has to build more of, and neither measure needs it.

    Async because the real implementation is an HTTP call to the CRM. The fake
    below answers from memory and is still async, so swapping it for the real
    one changes no caller.

    THREE THINGS AN IMPLEMENTATION OWES, and they are part of the ask:

      1. The window is HALF-OPEN, [since, until). Consecutive windows then tile
         without counting a row on the boundary twice, which is what a rolling
         measure run every morning does.
      2. Rows come back IN THE ORDER THEY WERE RECORDED, oldest first. Item 144
         has no other way to tell a resubmission from the judgement it followed:
         the row carries no link and both rows carry the same note timestamp.
      3. `since` after `until` is refused, not answered with an empty list. An
         empty list is indistinguishable from "this rep wrote nothing", and
         that reads as no evidence rather than as a bug.
    """

    async def rows_between(
        self,
        tenant: str,
        *,
        since: datetime,
        until: datetime,
        author_id: int | None = None,
    ) -> Sequence[JudgementRow]:
        """This tenant's rows with `since <= note_created_at < until`, oldest
        first; `author_id` narrows to one salesperson. Empty is a legitimate
        answer and means exactly that: nothing was judged in the window."""
        ...


def configured_path() -> Path | None:
    """The invented rows' path from the environment, or None when unset.

    None is the ordinary case and is not an error: there is no store until the
    backend answers, and CI never sets this. Blank is treated as unset -- an
    exported-but-empty variable is how a shell says "I did not set this".
    """
    raw = os.environ.get(JUDGEMENT_ROWS_PATH_ENV, "").strip()
    return Path(raw) if raw else None


class FileJudgementStore:
    """A JudgementStore over invented rows in a JSON file. A FAKE, and named one.

    ONE OBJECT, TENANT AT THE TOP:

        {"tenant-a": [ {row}, {row}, ... ], "tenant-b": [ ... ]}

    The tenant is the CONTAINER and not a column, because that is what it is in
    the CRM: each tenant has its own database, and a `tenant` field on the row
    would be a field the backend does not have and we would have to explain.
    List order is the order the rows were recorded, which is the ordering the
    Protocol promises.

    THE FILE IS READ ONCE, at construction. No request reads disk -- the same
    rule core/tenant_config.py holds for tenant files -- so a slow or vanished
    file is a startup failure and never a judgement that hangs.
    """

    def __init__(self, rows_by_tenant: dict[str, list[JudgementRow]]) -> None:
        self._rows = rows_by_tenant

    @classmethod
    def from_path(cls, path: Path) -> FileJudgementStore:
        """Load and validate every row in `path`.

        Refuses, in this order: a path inside the repository, a file that
        cannot be read, a document that is not one object of lists, and a row
        that is not a row. The repository check is the same guard the scored
        set has: a real export of these rows names real leads and real
        salespeople, and the easiest way for one to be committed is for the
        loader to accept a path that made that convenient.
        """
        resolved = path.expanduser().resolve()
        if resolved.is_relative_to(REPO_ROOT):
            raise JudgementStoreError(
                "refusing judgement rows inside the repository. "
                "A real export names real leads and real people -- keep it out."
            )
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            # The exception TYPE, never its message: an OSError's message is the
            # operating system's, and what it decides to include is not ours.
            raise JudgementStoreError(
                f"cannot read the judgement rows: {type(exc).__name__}"
            ) from None
        try:
            document = json.loads(raw)
        except ValueError:
            # from None and no decoder message: a JSONDecodeError carries the
            # offending document on `.doc`.
            raise JudgementStoreError("judgement rows: not one JSON object") from None
        if not isinstance(document, dict):
            raise JudgementStoreError("judgement rows: not one JSON object")

        rows_by_tenant: dict[str, list[JudgementRow]] = {}
        position = 0
        for tenant, entries in document.items():
            if not isinstance(entries, list):
                raise JudgementStoreError("judgement rows: a tenant is not a list")
            rows: list[JudgementRow] = []
            for entry in entries:
                position += 1
                rows.append(cls._row(position, entry))
            rows_by_tenant[tenant] = rows
        return cls(rows_by_tenant)

    @staticmethod
    def _row(position: int, entry: object) -> JudgementRow:
        """One entry as a row, or a refusal naming its position in the file
        and the exception type -- never what the entry said, nor whose it is."""
        try:
            return JudgementRow.model_validate(entry)
        except ValueError as exc:
            raise JudgementStoreError(
                f"judgement rows: row {position}: {type(exc).__name__}"
            ) from None

    async def rows_between(
        self,
        tenant: str,
        *,
        since: datetime,
        until: datetime,
        author_id: int | None = None,
    ) -> Sequence[JudgementRow]:
        """The Protocol's read, answered from memory.

        A tenant with no rows answers empty rather than raising: a tenant that
        has judged nothing is an ordinary state, and the measures suppress on
        it for want of evidence, which is the right outcome and not an error.
        """
        if since.tzinfo is None or until.tzinfo is None:
            # Refused rather than coerced: a naive bound here is somebody's
            # local midnight, and guessing which would move a rep's whole day.
            raise JudgementStoreError("since and until must be timezone-aware")
        if since > until:
            raise JudgementStoreError("since is after until")
        return [
            row
            for row in self._rows.get(tenant, ())
            if since <= row.note_created_at < until
            and (author_id is None or row.author_id == author_id)
        ]


def load_configured_store() -> FileJudgementStore | None:
    """The invented rows, read now, when their variable is set; else None.
    Called once, by the lifespan (register item 155)."""
    path = configured_path()
    return None if path is None else FileJudgementStore.from_path(path)


def get_judgement_store(request: Request) -> JudgementStore:
    """The store the lifespan loaded, for the routes that read one.

    Nothing is read here, so no request reads disk. None loaded is a 503,
    never an empty answer: a brief computed over no rows would tell a manager
    they had a quiet month when nobody had wired the store up. Tests override
    this FastAPI dependency, as they do every other seam.
    """
    store = getattr(request.app.state, "judgement_store", None)
    if store is None:
        raise BriefStoreUnavailable()
    return store
