"""Sample output contract — Lead, v1.

Versioned schema for a lead as our service. This is a SAMPLE to build and test the validator
against; the real lead shape is confirmed when the backend tool client is built. Version in the filename so a shape change ships as a new
file, not a silent edit.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LeadV1(BaseModel):
    # forbid unexpected fields: if the backend adds/renames something, we want
    # to KNOW (validation fails loudly) rather than silently ignore it.
    model_config = ConfigDict(extra="forbid")

    id: int
    name: str
    status: str