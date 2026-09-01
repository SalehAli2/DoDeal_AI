"""FakeLeadsClient — the dict-backed stand-in for the tool layer.

Lives here, not in one test module, because the route tests, the pipeline tests
and the later scoring tests all need it. Injected the way every other seam in
this repo is: app.dependency_overrides[get_leads_client] = lambda: fake.

It is structurally compatible with LeadsClient's three methods and takes a
TenantScope, so mypy checks the substitution here (tests/helpers is in the mypy
scope; tests/unit is not).

IT FAILS THE WAY THE REAL CLIENT FAILS. `raise_on` maps a method name to the
exception it should raise, and the exception a test reaches for is
ExternalCallError -- because that is what LeadsClient actually raises for a
missing lead today. The watchdog collapses 404, 500 and timeout into one type
(audit H2, step 4), so a fake that raised a tidy "NotFound" would let the
pipeline be written against a distinction the real client cannot make.

The fake DOES record the scope it was called with, so a test can prove the
tenant reaching the tool layer is the one the gates verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dodeal_ai.core.context import TenantScope
from dodeal_ai.schemas.lead import Lead, LeadNote


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

    def _record(self, method: str, scope: TenantScope, lead_id: int | None) -> None:
        self.calls.append(
            RecordedFetch(method=method, tenant=scope.tenant, lead_id=lead_id)
        )
        failure = self.raise_on.get(method)
        if failure is not None:
            raise failure

    async def get_leads(self, scope: TenantScope) -> list[Lead]:
        self._record("get_leads", scope, None)
        return list(self.leads.values())

    async def get_lead(self, scope: TenantScope, lead_id: int) -> Lead:
        self._record("get_lead", scope, lead_id)
        return self.leads[lead_id]

    async def get_lead_notes(self, scope: TenantScope, lead_id: int) -> list[LeadNote]:
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
