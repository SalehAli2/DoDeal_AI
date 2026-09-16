"""FakeLeadsClient — the dict-backed stand-in for the tool layer.

Lives here, not in one test module, because the route tests, the pipeline tests
and the later scoring tests all need it. Injected the way every other seam in
this repo is: app.dependency_overrides[get_leads_client] = lambda: fake.

It is structurally compatible with LeadsClient's three methods and takes a
TenantScope, so mypy checks the substitution here (tests/helpers is in the mypy
scope; tests/unit is not).

IT FAILS THE WAY THE REAL CLIENT FAILS. `raise_on` maps a method name to the
exception it should raise: ExternalCallError for a failure that outlived its
retry, or one of the typed 4xx errors in tools/errors.py (register item 89).

The fake DOES record the scope it was called with, so a test can prove the
tenant reaching the tool layer is the one the gates verified.

TWO WAYS TO BUILD ONE. `lead()`/`note()` hand-build the two or three records a
unit test reasons about by name. `load_fixture_client()` loads the whole
vendored fake-CRM corpus (`tests/fixtures/fake_crm/tenant-a.json`) through the
SAME schemas the real client parses backend JSON with, so the corpus is proven
to match the shape the backend returns rather than assumed to. The eval suite
needs the corpus; every existing test keeps its hand-built records.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from dodeal_ai.core.context import TenantScope
from dodeal_ai.schemas.lead import Lead, LeadNote

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "fake_crm" / "tenant-a.json"
)


@dataclass(frozen=True, slots=True)
class RecordedFetch:
    method: str
    tenant: str
    lead_id: int | None


@dataclass
class FakeLeadsClient:
    """Dict-backed leads and notes. `notes` maps lead_id -> that lead's notes,
    newest first, exactly as the backend returns them."""

    leads: dict[int, Lead] = field(default_factory=dict)
    notes: dict[int, list[LeadNote]] = field(default_factory=dict)
    raise_on: dict[str, Exception] = field(default_factory=dict)
    calls: list[RecordedFetch] = field(default_factory=list)
    # Provenance, set only by load_fixture_client: the generator seed the
    # vendored corpus was produced with. None for a hand-built fake, which has
    # no generator and no seed to be wrong about.
    seed: str | None = None
    # How many records in the vendored corpus the schemas REJECTED and the
    # loader therefore did not hold. Zero for a hand-built fake. Pinned by
    # tests/unit/test_fake_crm_fixture.py so a regeneration cannot raise it
    # quietly.
    skipped_invalid_leads: int = 0

    def _record(self, method: str, scope: TenantScope, lead_id: int | None) -> None:
        self.calls.append(
            RecordedFetch(method=method, tenant=scope.tenant, lead_id=lead_id)
        )
        failure = self.raise_on.get(method)
        if failure is not None:
            raise failure

    async def get_leads(
        self, scope: TenantScope, *, deadline: float | None = None
    ) -> list[Lead]:
        self._record("get_leads", scope, None)
        return list(self.leads.values())

    async def get_lead(
        self, scope: TenantScope, lead_id: int, *, deadline: float | None = None
    ) -> Lead:
        self._record("get_lead", scope, lead_id)
        return self.leads[lead_id]

    async def get_lead_notes(
        self, scope: TenantScope, lead_id: int, *, deadline: float | None = None
    ) -> list[LeadNote]:
        self._record("get_lead_notes", scope, lead_id)
        return list(self.notes.get(lead_id, []))


def lead(lead_id: int, **overrides: object) -> Lead:
    """A minimal valid lead. Only `id` is required by the schema; everything
    else is nullable, which is the real backend's shape."""
    return Lead(id=lead_id, **overrides)


def note(
    note_id: int,
    text: str,
    *,
    author_id: int = 27,
    author: str | None = "Jane Doe",
    created_at: str = "2026-01-02T09:00:00+00:00",
) -> LeadNote:
    """A note. `note` is a required non-nullable str on LeadNote and "" is
    valid, which is exactly why the thin-evidence rule exists."""
    return LeadNote(
        id=note_id,
        note=text,
        author=author,
        author_id=author_id,
        createdAt=created_at,
    )


def load_fixture_client(path: Path = FIXTURE_PATH) -> FakeLeadsClient:
    """The whole vendored fake-CRM corpus as a FakeLeadsClient.

    Every lead goes through `Lead` and every note through `LeadNote` -- the
    same models `LeadsClient` validates real backend JSON with. A corpus that
    cannot be parsed by them is not a corpus the pipeline could ever have run
    on, and this raises at load time rather than at the note that breaks.

    Notes are kept in the file's own per-lead order (newest first, which is the
    order the backend returns and the order Design A's page-one match depends
    on). The loader never sorts, dedupes or repairs: what it hands back is what
    the vendor generated, or it raises.

    `timeline_events` is a SEPARATE top-level key in the file and is not loaded
    here. It is not what `GET /leads/{id}/notes` returns, and folding it into
    `notes` would silently grow the 127-note corpus the campaign counts.

    ONE LEAD IN THE CORPUS DOES NOT VALIDATE. Lead 1661 carries
    `bookedAmount: "1,250,000"` -- a formatted string where the schema says
    `float | None`, and `schemas/lead.py` says in as many words that this
    field's real type is UNCONFIRMED. The loader skips it and counts it rather
    than doing either of the two things that would hide it: coercing the string
    (which would invent a parse rule -- comma as a thousands separator, not the
    decimal comma half the world writes -- for a field nobody has confirmed) or
    relaxing `Lead` (src/ is not this piece's to change). The count is pinned in
    tests/unit/test_fake_crm_fixture.py, so a second bad lead fails a test
    instead of vanishing. See CAMPAIGN_REPORT.md, Piece I.1, for the lead.

    A skipped lead does NOT cost its notes. The two live under different
    top-level keys, and 1661 has none in any case; dropping notes over an
    unrelated money field would shrink the corpus for no reason.
    """
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    leads: dict[int, Lead] = {}
    skipped = 0
    for raw in payload["leads"]:
        try:
            item = Lead.model_validate(raw)
        except ValidationError:
            skipped += 1
            continue
        leads[item.id] = item

    notes = {
        int(lead_id): [LeadNote.model_validate(raw) for raw in raw_notes]
        for lead_id, raw_notes in payload["notes"].items()
    }
    return FakeLeadsClient(
        leads=leads,
        notes=notes,
        seed=payload["seed"],
        skipped_invalid_leads=skipped,
    )


def load_fixture_timeline_events(
    path: Path = FIXTURE_PATH,
) -> dict[int, list[LeadNote]]:
    """The corpus's `timeline_events`, keyed by lead id, validated as LeadNotes.

    SEPARATE FROM load_fixture_client ON PURPOSE. These 27 records are the
    machine-written half of the fixture -- assignments, stage changes, import
    traces -- and folding them into `notes` would grow the 127-note corpus the
    campaign counts and feed CRM timeline text to the pipeline as though a
    salesperson had typed it. They validate as `LeadNote` because that is the
    shape the backend would return them in, and a caller that WANTS to exercise
    the system_event path (the eval suite does) builds its own client from them.

    Returned as a mapping rather than a client for the same reason: handing back
    a FakeLeadsClient with timeline events sitting in the `notes` slot would be
    the very conflation this function exists to avoid.
    """
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return {
        int(lead_id): [LeadNote.model_validate(raw) for raw in raw_events]
        for lead_id, raw_events in payload["timeline_events"].items()
    }
