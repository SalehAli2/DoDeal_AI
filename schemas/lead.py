"""Confirmed lead response schema (source: backend integration, Waqas).

The lead-list response is wrapped:

    {
        "status": true,
        "posts": {
            <pagination fields>,
            "data": [ <lead>, ... ]
        }
    }

Leads are read from `posts.data`. Fields below are the confirmed lead fields.
`extra="ignore"` on the lead model tolerates additional fields the backend may
send, since this is an external response we do not control. The meaning of
`feedback` is not yet confirmed and is modelled as an optional string only.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Lead(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    leadName: str | None = None
    leadContact: str | None = None
    leadEmail: str | None = None
    leadType: str | None = None
    project: str | None = None
    leadStatus: str | None = None
    leadSource: str | None = None
    feedback: str | None = None
    priority: str | None = None
    country: str | None = None
    assignedToManager: str | None = None
    assignedToSales: str | None = None
    booked_amount: float | None = None
    creationDate: str | None = None
    lastEdited: str | None = None


class LeadPosts(BaseModel):
    """The `posts` object: pagination metadata plus the lead list. Pagination
    fields vary and are not modelled individually; `data` is what we depend on."""

    model_config = ConfigDict(extra="ignore")

    data: list[Lead]


class LeadListResponse(BaseModel):
    """The full wrapped response. Leads are at `posts.data`."""

    model_config = ConfigDict(extra="ignore")

    status: bool
    posts: LeadPosts
