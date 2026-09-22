"""TenantConfig — the per-tenant rubric seam, and the ONLY import path for a
weight, a threshold, a cap or a TTL in this unit.

Nothing else may hardcode these numbers. A weight that appears in two places
drifts; a weight that appears in a prompt string cannot be changed without
changing the prompt version. So:

  - Weights, boundaries and thresholds live HERE and are read through
    get_tenant_config(). Scoring code reads config.weights; it never carries a
    literal 25.
  - **Weights never appear in prompt text.** The model answers yes or no to a
    fixed list of checks; it is told neither what a component is worth nor
    which component a check belongs to (register item 131). If the rubric's
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

`enforcement_mode` (register item 142) is the tenant's setting and the ONLY
place a mode is chosen. It BRANCHES now: decide.py::enforcement derives the
verdict on every judgement from it, and the mode is stamped on the judgement so
a tenant that changes it later does not change what an old judgement meant.
The vocabulary itself lives in schemas.py, with the other codes the CRM reads.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from dodeal_ai.core.tenant_config import (
    ResolvedSection,
    resolve_section,
    tenant_section,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    MAX_NOTE_TEXT_CHARS,
    Band,
    CheckName,
    ComponentName,
    EnforcementMode,
    MissingComponent,
    NoteType,
)

# Re-exported: EnforcementMode is a response vocabulary and lives in
# schemas.py, but config.py is where a tenant's mode is chosen and where every
# caller has always imported it from. Named here so ruff keeps the import.
__all__ = [
    "UNIT_A_SECTION",
    "EnforcementMode",
    "TenantConfig",
    "config_of",
    "get_tenant_config",
    "resolve_tenant_config",
    "section_of",
]


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
# Which checks belong to which component (register item 131). The model
# answers facts; this table is what turns those facts into marks, and it
# lives here for the same reason the weights do: a rubric decision the
# business owns, in one place, never spread through the scoring code.
_CHECKS_BY_COMPONENT: Mapping[ComponentName, tuple[CheckName, ...]] = MappingProxyType(
    {
        ComponentName.WHAT_HAPPENED: (
            CheckName.WH_OUTCOME,
            CheckName.WH_ACTION,
        ),
        ComponentName.CLIENT_SAID: (
            CheckName.CS_PRESENT,
            CheckName.CS_OWN_TERMS,
        ),
        ComponentName.NEXT_STEP_DATE: (
            CheckName.NS_ACTION,
            CheckName.NS_DATE,
            CheckName.NS_CLOSURE,
        ),
        ComponentName.DEAL_SPECIFICS: (
            CheckName.DS_FIGURES,
            CheckName.DS_SUBJECT,
            CheckName.DS_TIMING,
        ),
        ComponentName.CLARITY: (
            CheckName.CL_READABLE,
            CheckName.CL_SUBSTANCE,
        ),
    }
)

# How many true checks earn a component its full weight, where that is not all
# of them. NEXT_STEP_DATE has three checks but two ordinary ones: ns_closure is
# a bypass (scoring.py gives full marks whatever the others say), so action plus
# date already earns the full weight and closure adds nothing to a table entry.
_FULL_MARKS_AT: Mapping[ComponentName, int] = MappingProxyType(
    {ComponentName.NEXT_STEP_DATE: 2}
)


def _derive_marks(
    weights: Mapping[ComponentName, int],
    checks_by_component: Mapping[ComponentName, tuple[CheckName, ...]],
) -> Mapping[ComponentName, tuple[int, ...]]:
    """The mark for a component by how many of its checks came back true,
    derived from its weight (register item 131).

    An even split, rounded half up in integers: index 0 is 0, the full-marks
    count is exactly the weight, and the tuple is one longer than the check
    list. A tenant that changes a weight gets its marks with it, so the two
    cannot drift and no mark table is a second thing to keep in step. A
    component with no weight is skipped; _check refuses the weights first.
    """
    marks: dict[ComponentName, tuple[int, ...]] = {}
    for component, checks in checks_by_component.items():
        if component not in weights:
            continue
        weight = weights[component]
        full = max(1, _FULL_MARKS_AT.get(component, len(checks)))
        marks[component] = tuple(
            (weight * min(true_count, full) * 2 + full) // (2 * full)
            for true_count in range(len(checks) + 1)
        )
    return MappingProxyType(marks)


# A no_contact note ("called, no answer") cannot report what the client said,
# has no deal specifics to give, and the attempt IS what happened -- there is
# no outcome beyond it to record. Those components are SUPPRESSED for it --
# their weight leaves the denominator entirely, which for no_contact is 35.
# Scoring them 0 instead would cap an honest no-contact note and teach
# salespeople to pad notes.
_SUPPRESSED_COMPONENTS_BY_TYPE: Mapping[NoteType, frozenset[ComponentName]] = (
    MappingProxyType(
        {
            NoteType.NO_CONTACT: frozenset(
                {
                    ComponentName.WHAT_HAPPENED,
                    ComponentName.CLIENT_SAID,
                    ComponentName.DEAL_SPECIFICS,
                }
            ),
        }
    )
)

# Which components the vagueness pass may legitimately report missing, per
# type. THE SAME RULE AS THE TABLE ABOVE, and it has to be: a component nobody
# may be asked about is one nobody may be marked down on. no_contact's two
# tables agree member for member (deal_specifics has no MissingComponent
# counterpart), and they diverged once -- register item 130 narrowed this one
# and left what_happened scored, which cost a no-contact note 25 of 60 on
# something its author could never be asked to fix.
_ALL_MISSING: frozenset[MissingComponent] = frozenset(MissingComponent)

_ALLOWED_MISSING_BY_TYPE: Mapping[NoteType, frozenset[MissingComponent]] = (
    MappingProxyType(
        {
            NoteType.NO_CONTACT: frozenset(
                {
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
    checks_by_component: Mapping[ComponentName, tuple[CheckName, ...]]
    # Derived from weights and checks in __post_init__, never set by a caller.
    marks_by_true_count: Mapping[ComponentName, tuple[int, ...]] = field(init=False)
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

    # Register item 144. How many rows a per-rep measure needs before it is
    # reported at all; below it the measure is SUPPRESSED with a reason. 10 is
    # provisional and deliberately not small: the measure it protects is shown
    # to a manager as a standard. Too low and a rep with three notes is given
    # an average that is noise; too high and a real team never sees one.
    measure_evidence_floor: int
    # The rolling window every per-rep measure is computed over, in days. 30 as
    # the business document asks. Too short and a quiet week suppresses
    # everything; too long and a rep who improved last week still reads badly.
    rolling_window_days: int

    # Register item 142: what the tenant asked us to do with a judgement it
    # does not like. Read once per judgement and stamped on the block; a
    # tenant that moves to strict never changes an old judgement's meaning.
    enforcement_mode: EnforcementMode

    # Register item 156: the IANA zone the tenant's days are counted in. Every
    # window is whole local days and every printed period is local dates, so a
    # rep's "yesterday" ends at their midnight, not at UTC's. A wrong zone moves
    # a whole evening's notes into the next day. Not part of what a mark means,
    # so changing it bumps no version.
    timezone: str
    config_version: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "marks_by_true_count",
            _derive_marks(self.weights, self.checks_by_component),
        )
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
    checks_by_component=_CHECKS_BY_COMPONENT,
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
    measure_evidence_floor=10,  # provisional -- see the field's comment
    rolling_window_days=30,
    # Every tenant launches advisory and no team moves to strict before the
    # calibration target is met, which nothing in this service can check. So
    # advisory is the default and only a tenant file may say otherwise.
    enforcement_mode=EnforcementMode.ADVISORY,
    timezone="Asia/Dubai",
    # -4: register item 142 changed the enforcement_mode vocabulary, so a file
    # saying "blocking" no longer parses. The rubric did not move and
    # RUBRIC_VERSION did not either -- this stamp is what makes an old
    # judgement, made under the old vocabulary, still identifiable.
    config_version="tenant-cfg-default-4",
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
    # Register item 144. ge=1, not ge=0: a floor of 0 reports a measure over no
    # rows at all, which is the exact thing the floor exists to refuse. These
    # two are refused HERE and not in _check -- the field bound is the whole
    # invariant, and a second copy in _check would be a line no input reaches.
    measure_evidence_floor: int | None = Field(default=None, ge=1)
    rolling_window_days: int | None = Field(default=None, gt=0)
    enforcement_mode: EnforcementMode | None = None
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        """An IANA name the zone database knows; anything else is refused
        rather than read as UTC. Fixed message: never the value."""
        if value is None:
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("timezone") from None
        return value

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
    # Summing to 100 is not enough (register item 137). deal_specifics is
    # suppressed for EVERY type while Q13 is open, so a rubric that puts all
    # 100 on it leaves every judgement with a denominator of 0 -- accepted at
    # startup, then a ValueError on every note. Refused here instead.
    if not any(
        weight
        for component, weight in config.weights.items()
        if component is not ComponentName.DEAL_SPECIFICS
        or config.deal_specifics_applicable
    ):
        raise ValueError("weights_applicable")
    bands = [band for band, _ in config.band_boundaries]
    uppers = [upper for _, upper in config.band_boundaries]
    ascending = uppers == sorted(set(uppers)) and uppers[0] >= 0
    if bands != list(Band) or not ascending or uppers[-1] != 100:
        raise ValueError("band_boundaries")
    if set(config.checks_by_component) != set(ComponentName):
        raise ValueError("checks_by_component")
    all_checks = [c for checks in config.checks_by_component.values() for c in checks]
    if sorted(all_checks) != sorted(CheckName):
        raise ValueError("checks_coverage")
    if not config.flag_threshold < config.accept_threshold:
        raise ValueError("thresholds")
    if not config.min_note_chars < config.max_note_chars:
        raise ValueError("note_bounds")


def parse_unit_a_section(raw: object) -> TenantConfig:
    """The `unit_a` section as a TenantConfig, or ValueError: a failed field
    (pydantic's ValidationError) or a broken invariant (_check)."""
    return TenantConfigFile.model_validate(raw).build()


def get_tenant_config(tenant: str) -> TenantConfig:
    """This tenant's STARTUP rules: its file's `unit_a` section, else the default.

    For the scripts, which run with no store. A route never calls this: it
    resolves once at entry with `resolve_tenant_config`, which puts the
    runtime override first (register item 97), and hands the result down.
    """
    section = tenant_section(tenant, UNIT_A_SECTION)
    return section if isinstance(section, TenantConfig) else _DEFAULT_CONFIG


def config_of(resolved: ResolvedSection) -> TenantConfig:
    """A resolved `unit_a` section as this unit's config; the default when the
    resolution found neither an override nor a file."""
    value = resolved.value
    return value if isinstance(value, TenantConfig) else _DEFAULT_CONFIG


async def resolve_tenant_config(tenant: str) -> TenantConfig:
    """The rules in force for `tenant`: the runtime override, else the file,
    else the default (register item 97). Called once per request, at entry."""
    return config_of(await resolve_section(tenant, UNIT_A_SECTION))


def section_of(config: TenantConfig) -> dict[str, object]:
    """A config as the `unit_a` section that produces it -- every field the
    section may set, as plain JSON. What the admin route shows as in force;
    parse_unit_a_section(section_of(config)) gives the same config back."""
    return {
        name: _plain(getattr(config, name)) for name in TenantConfigFile.model_fields
    }


def _plain(value: object) -> object:
    """Enums to their values, mappings to dicts, sets to sorted lists."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(_plain(key)): _plain(item) for key, item in value.items()}
    if isinstance(value, frozenset):
        return sorted(str(_plain(item)) for item in value)
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value
