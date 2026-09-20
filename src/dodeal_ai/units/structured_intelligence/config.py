"""TenantConfig — the per-tenant rubric seam, and the ONLY import path for a
weight, a threshold, a cap or a TTL in this unit.

Nothing else may hardcode these numbers. A weight that appears in two places
drifts; a weight that appears in a prompt string cannot be changed without
changing the prompt version. So:

  - Weights, boundaries and thresholds live HERE and are read through
    get_tenant_config(). Scoring code reads config.weights; it never carries a
    literal 25.
  - **Weights never appear in prompt text.** The model returns a mark per
    component and does not know what a component is worth. If the rubric's
    arithmetic were in the prompt, changing a weight would silently change what
    the model was asked to do, and every past judgement would become
    incomparable in a way no version stamp records.

THE PER-TENANT FILES (register item 97). core/tenant_config.py loads each
`<tenant>.json` at startup; this unit's `unit_a` section is validated into a
TenantConfig over the default, with `config_version` required. A tenant with
no file, or no `unit_a` section, gets the default.
`config_version` is stamped on every judgement, so judgements made under the
default remain identifiable and are never rescored.

The values are placeholders in the same sense as the cost caps in core/config.py
(ASSUMPTIONS §8.1): structurally correct, numerically provisional.

`enforcement_mode` is carried but does not branch: advisory and blocking behave
identically here, because ENFORCEMENT IS THE CRM'S. We return a decision; what
the CRM does with `prompt_clarification` is its own policy. The field exists so
a tenant's intent is recorded on the judgement rather than inferred later.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from pydantic import BaseModel, ConfigDict, Field

from dodeal_ai.core.tenant_config import tenant_section
from dodeal_ai.units.structured_intelligence.schemas import (
    MAX_NOTE_TEXT_CHARS,
    Band,
    ComponentName,
    MissingComponent,
    NoteType,
)


class EnforcementMode(StrEnum):
    """Whether the tenant treats a clarification request as advice or as a
    block. Identical behaviour in this service -- see the module docstring."""

    ADVISORY = "advisory"
    BLOCKING = "blocking"


# Weights sum to 100 so the full-applicability denominator IS 100 and a total
# needs no rescaling in the common case. A test guards the sum: a rubric whose
# weights drift off 100 still produces a correct percentage (the denominator is
# computed, not assumed), but the "out of 100" reading every human applies to
# it would quietly stop being true.
_WEIGHTS: Mapping[ComponentName, int] = MappingProxyType(
    {
        ComponentName.WHAT_HAPPENED: 25,
        ComponentName.CLIENT_SAID: 20,
        ComponentName.NEXT_STEP_DATE: 25,
        ComponentName.DEAL_SPECIFICS: 20,
        ComponentName.CLARITY: 10,
    }
)

# (band, inclusive upper bound), ascending. poor <=39 · fair 40-69 ·
# good 70-84 · excellent 85-100. Stored as data and read by band_for() so the
# split points are testable as behaviour, not just as numbers.
_BAND_BOUNDARIES: tuple[tuple[Band, int], ...] = (
    (Band.POOR, 39),
    (Band.FAIR, 69),
    (Band.GOOD, 84),
    (Band.EXCELLENT, 100),
)

# A no_contact note ("called, no answer") cannot report what the client said,
# and has no deal specifics to give. Those components are SUPPRESSED for it --
# their weight leaves the denominator entirely. Scoring them 0 instead would
# cap an honest no-contact note at 60/100 and teach salespeople to pad notes.
_SUPPRESSED_COMPONENTS_BY_TYPE: Mapping[NoteType, frozenset[ComponentName]] = (
    MappingProxyType(
        {
            NoteType.NO_CONTACT: frozenset(
                {ComponentName.CLIENT_SAID, ComponentName.DEAL_SPECIFICS}
            ),
        }
    )
)

# Which components the vagueness pass may legitimately report missing, per
# type. Same logic: asking a no_contact note "what did the client say?" is a
# question its author cannot answer.
_ALL_MISSING: frozenset[MissingComponent] = frozenset(MissingComponent)

_ALLOWED_MISSING_BY_TYPE: Mapping[NoteType, frozenset[MissingComponent]] = (
    MappingProxyType(
        {
            NoteType.NO_CONTACT: frozenset(
                {
                    MissingComponent.WHAT_HAPPENED,
                    MissingComponent.NEXT_STEP_WITH_DATE,
                }
            ),
            NoteType.CALLBACK: _ALL_MISSING,
            NoteType.DISCOVERY: _ALL_MISSING,
            NoteType.VIEWING: _ALL_MISSING,
            NoteType.NEGOTIATION: _ALL_MISSING,
            NoteType.WON_LOST: _ALL_MISSING,
        }
    )
)


@dataclass(frozen=True, slots=True)
class TenantConfig:
    """One tenant's rubric and limits. Frozen, and every container on it is
    immutable (MappingProxyType / frozenset / tuple), so a caller cannot mutate
    the shared default instance and change another request's scoring."""

    weights: Mapping[ComponentName, int]
    band_boundaries: tuple[tuple[Band, int], ...]
    suppressed_components_by_type: Mapping[NoteType, frozenset[ComponentName]]
    allowed_missing_by_type: Mapping[NoteType, frozenset[MissingComponent]]

    # Register item 132: a note below the length floor that is a known outcome
    # is judged, not asked "what happened". Codes take a trailing attempt number
    # (na1, cb2); phrases match the whole stripped note. Tables, not a model
    # call: over half of real notes are this short. Empty recognises nothing.
    # Fillers ("tmrw", "am") may follow a code; every word must be a code or a
    # filler. All three tables are stored casefolded (__post_init__ folds them).
    short_note_codes: frozenset[str]
    short_note_phrases: frozenset[str]
    short_note_fillers: frozenset[str]

    accept_threshold: int
    flag_threshold: int

    # --- ASSUMPTION[Q13] ---------------------------------------------------
    # The rubric was written for two business lines with different "deal
    # specifics" (budget/bedrooms/area for sales; term/handover for leasing).
    # No field on the lead is CONFIRMED to carry the business line -- `leadFor`
    # and `enquiryType` both look plausible and neither is confirmed, and
    # inventing a backend field is not allowed. So the component is switched
    # OFF: business_line_field stays None, deal_specifics_applicable stays
    # False, and deal_specifics is suppressed for every type -- its 20 points
    # leave the denominator (100 -> 80).
    #
    # CORRECTION PATH when the backend confirms the field:
    #   1. Set business_line_field to its confirmed name and flip
    #      deal_specifics_applicable to True.
    #   2. Add the per-business-line checklist the prompt needs to mark it.
    #   3. Bump config_version.
    #   4. NEVER rescore history. Old judgements carry the old config_version
    #      and a denominator of 80; new ones carry 100. They are distinguishable
    #      by the stamp, which is the whole reason the stamp exists.
    business_line_field: str | None
    deal_specifics_applicable: bool

    min_note_chars: int
    min_note_tokens: int
    # A structural bound, counted after strip, same place as the thin gate.
    # The SOFT limit: over it, the note is suppressed not_scorable /
    # note_too_long with nothing reserved and nothing spent. The schema's
    # MAX_NOTE_TEXT_CHARS is the hard ceiling on the direct route's body and is
    # a 422; this one is the rubric's own bound and applies to BOTH routes,
    # because a note too long to be one interaction is too long whichever way
    # its text reached us. No inline default: no field on this dataclass
    # carries one, and one here would be an ordering error. The value lives in
    # _DEFAULT_CONFIG beside min_note_chars.
    max_note_chars: int

    clarification_cap: int
    rate_limit_per_hour: int
    # Register item 66: a second ceiling on the same guard, its own window (a
    # calendar day, fixed in pipeline.py -- only the cap is per-tenant). Catches
    # a subject who stays just under the hourly cap all day; a value of 0 would
    # withhold every prompt regardless of the hourly count.
    rate_limit_per_day: int
    rate_limit_window_seconds: int

    attempt_ttl_seconds: int
    idempotency_ttl_seconds: int

    enforcement_mode: EnforcementMode
    config_version: str

    def __post_init__(self) -> None:
        # The three short-note tables are folded here, the one place every
        # TenantConfig passes through, so a hand-built config matches too.
        for name in ("short_note_codes", "short_note_phrases", "short_note_fillers"):
            table = getattr(self, name)
            object.__setattr__(self, name, frozenset(w.casefold() for w in table))

    def band_for(self, total: int) -> Band:
        """Derive the band from a total. THE only way a band is produced --
        a band is never accepted from a model, a caller, or a stored value.

        Boundaries are inclusive upper bounds in ascending order; the last one
        catches everything at or below 100. A total above the final bound
        (which compute cannot produce) still returns the top band rather than
        raising: this is a labelling function, not a validator, and the bound
        on `total` is enforced where the total is computed.
        """
        for band, upper in self.band_boundaries:
            if total <= upper:
                return band
        return self.band_boundaries[-1][0]


_DEFAULT_CONFIG = TenantConfig(
    weights=_WEIGHTS,
    band_boundaries=_BAND_BOUNDARIES,
    suppressed_components_by_type=_SUPPRESSED_COMPONENTS_BY_TYPE,
    allowed_missing_by_type=_ALLOWED_MISSING_BY_TYPE,
    short_note_codes=frozenset({"na", "wa", "cb"}),
    short_note_phrases=frozenset(
        {
            "not interested",
            "no answer",
            "no reply",
            "wrong number",
            "لا يرد",
            "مش مهتم",
        }
    ),
    short_note_fillers=frozenset(
        {"tmrw", "today", "bkra", "bokra", "am", "pm", "again", "بكرة", "النهاردة"}
    ),
    accept_threshold=70,
    flag_threshold=40,
    business_line_field=None,  # ASSUMPTION[Q13] -- see TenantConfig above
    deal_specifics_applicable=False,  # ASSUMPTION[Q13]
    min_note_chars=15,
    min_note_tokens=3,
    max_note_chars=2000,  # provisional -- the real sample's longest note is 340
    clarification_cap=1,
    rate_limit_per_hour=3,
    rate_limit_per_day=10,
    rate_limit_window_seconds=3600,
    attempt_ttl_seconds=21600,  # 6h
    idempotency_ttl_seconds=86400,  # 24h
    enforcement_mode=EnforcementMode.ADVISORY,
    config_version="tenant-cfg-default-3",
)


# This unit's section name in a `<tenant>.json` (core/tenant_config.py).
UNIT_A_SECTION = "unit_a"


class TenantConfigFile(BaseModel):
    """A tenant's `unit_a` section: the numbers it may set, over the default.

    The per-type rubric maps and the Q13 business-line switch are not here: they
    change what a component MEANS, not how it is weighted, and stay code.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    config_version: str = Field(min_length=1)
    weights: dict[ComponentName, int] | None = None
    band_boundaries: list[tuple[Band, int]] | None = None
    short_note_codes: frozenset[str] | None = None
    short_note_phrases: frozenset[str] | None = None
    short_note_fillers: frozenset[str] | None = None
    accept_threshold: int | None = Field(default=None, ge=0, le=100)
    flag_threshold: int | None = Field(default=None, ge=0, le=100)
    min_note_chars: int | None = Field(default=None, ge=1)
    min_note_tokens: int | None = Field(default=None, ge=1)
    max_note_chars: int | None = Field(default=None, ge=1, le=MAX_NOTE_TEXT_CHARS)
    clarification_cap: int | None = Field(default=None, ge=0)
    rate_limit_per_hour: int | None = Field(default=None, ge=0)
    rate_limit_per_day: int | None = Field(default=None, ge=0)
    rate_limit_window_seconds: int | None = Field(default=None, gt=0)
    attempt_ttl_seconds: int | None = Field(default=None, gt=0)
    idempotency_ttl_seconds: int | None = Field(default=None, gt=0)
    enforcement_mode: EnforcementMode | None = None

    def build(self) -> TenantConfig:
        """This file over the default, checked as a whole."""
        updates = {
            name: value
            for name, value in self.model_dump(exclude_none=True).items()
            if name not in {"weights", "band_boundaries"}
        }
        if self.weights is not None:
            updates["weights"] = MappingProxyType(dict(self.weights))
        if self.band_boundaries is not None:
            updates["band_boundaries"] = tuple(self.band_boundaries)
        config = dataclasses.replace(_DEFAULT_CONFIG, **updates)
        _check(config)
        return config


def _check(config: TenantConfig) -> None:
    """The invariants the default holds, held by every tenant's config too.
    Raises ValueError with a fixed message; the caller names the tenant."""
    if set(config.weights) != set(ComponentName) or any(
        weight < 0 for weight in config.weights.values()
    ):
        raise ValueError("weights")
    if sum(config.weights.values()) != 100:
        raise ValueError("weights_sum")
    bands = [band for band, _ in config.band_boundaries]
    uppers = [upper for _, upper in config.band_boundaries]
    ascending = uppers == sorted(set(uppers)) and uppers[0] >= 0
    if bands != list(Band) or not ascending or uppers[-1] != 100:
        raise ValueError("band_boundaries")
    if not config.flag_threshold < config.accept_threshold:
        raise ValueError("thresholds")
    if not config.min_note_chars < config.max_note_chars:
        raise ValueError("note_bounds")


def parse_unit_a_section(raw: object) -> TenantConfig:
    """The `unit_a` section as a TenantConfig, or ValueError: a failed field
    (pydantic's ValidationError) or a broken invariant (_check)."""
    return TenantConfigFile.model_validate(raw).build()


def get_tenant_config(tenant: str) -> TenantConfig:
    """This tenant's rubric and limits: its `unit_a` section, else the default.

    The pipeline's config is chosen here by the request's tenant (register item
    97), the same tenant the scope carries.
    """
    section = tenant_section(tenant, UNIT_A_SECTION)
    return section if isinstance(section, TenantConfig) else _DEFAULT_CONFIG
