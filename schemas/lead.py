"""Confirmed lead and note response schemas (source: backend integration guide).

Endpoints (base https://<tenant>.dodealcrm.com/api/service):
  - GET /leads              paginated list, newest first
  - GET /leads/{id}         single lead
  - GET /leads/{id}/notes   that lead's notes, newest first (data: [] if none,
    not an error)

The lead-list and notes-list responses share the same wrapper shape:

    { "status": bool, "data": [ <item>, ... ], "meta": { current_page,
      per_page, total, last_page } }

Leads are read from `data` directly, NOT `posts.data` -- that was an earlier,
incorrect assumption; see ASSUMPTIONS.md. `extra="ignore"` on every model
tolerates additional fields the backend may send, since the backend has
confirmed its data is frequently incomplete and may grow new fields; this is
an external response the service does not control.

Every lead field except `id` may be null and is modelled as optional, per the
integration guide. `bookedAmount`'s real type is unconfirmed (modelled as an
optional float; the backend may send null). `createdAt`/`updatedAt` are
confirmed ISO-8601 with a timezone offset; kept as `str` for now rather than
parsed to `datetime`, since nothing downstream needs them parsed yet.

Note fields are modelled more strictly than lead fields: only `author` is
confirmed nullable (null if the original author's account was deleted); the
guide does not say the rest may be absent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class PageMeta(BaseModel):
    """Pagination metadata shared by the leads and notes list responses."""

    model_config = ConfigDict(extra="ignore")

    current_page: int
    per_page: int
    total: int
    last_page: int


class Lead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    name: str | None = None
    phone: str | None = None
    email: str | None = None
    leadType: str | None = None
    enquiryType: str | None = None
    project: str | None = None
    status: str | None = None
    source: str | None = None
    feedback: str | None = None
    priority: str | None = None
    language: str | None = None
    leadFor: str | None = None
    country: str | None = None
    assignedToManager: int | None = None
    assignedToSales: int | None = None
    bookedAmount: float | None = None
    createdAt: str | None = None
    updatedAt: str | None = None


class LeadListResponse(BaseModel):
    """The lead-list response. Leads are at `data`."""

    model_config = ConfigDict(extra="ignore")

    status: bool
    data: list[Lead]
    meta: PageMeta


class LeadResponse(BaseModel):
    """The single-lead response (GET /leads/{id}). UNCONFIRMED shape: the
    integration guide documents the list envelope but not this one; modelled
    by analogy as {status, data} with `data` as a single lead, no `meta`
    (pagination does not apply to a single resource). See ASSUMPTIONS.md."""

    model_config = ConfigDict(extra="ignore")

    status: bool
    data: Lead


class LeadNote(BaseModel):
    """A single note on a lead. `author` is nullable: null when the original
    author's account has since been deleted."""

    model_config = ConfigDict(extra="ignore")

    id: int
    note: str
    author: str | None = None
    author_id: int
    createdAt: str


class LeadNotesResponse(BaseModel):
    """The lead-notes response for a single lead. A lead with no notes
    returns `data: []` with a 200, not an error."""

    model_config = ConfigDict(extra="ignore")

    status: bool
    data: list[LeadNote]
    meta: PageMeta
