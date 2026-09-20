"""TenantConfig: one frozen default for every tenant, weights that sum to 100,
band boundaries that split where the rubric says, and Q13's deal_specifics
switched off.
"""

from __future__ import annotations

import dataclasses
from types import MappingProxyType

import pytest

from dodeal_ai.units.structured_intelligence.config import (
    EnforcementMode,
    TenantConfig,
    _check,
    get_tenant_config,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Band,
    CheckName,
    ComponentName,
    MissingComponent,
    NoteType,
)


@pytest.fixture
def config() -> TenantConfig:
    return get_tenant_config("tenant-a")


# --- the seam --------------------------------------------------------------


def test_every_tenant_gets_the_same_instance_today() -> None:
    # No per-tenant store exists. Identity, not equality: proves there is one
    # shared frozen default rather than a per-call copy that could drift.
    assert get_tenant_config("tenant-a") is get_tenant_config("tenant-b")


def test_config_is_frozen(config: TenantConfig) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.accept_threshold = 50  # type: ignore[misc]


def test_containers_on_the_shared_default_are_immutable(config: TenantConfig) -> None:
    # Frozen alone would still let a caller mutate a dict field and change
    # another request's scoring, since the default is shared.
    with pytest.raises(TypeError):
        config.weights[ComponentName.CLARITY] = 99  # type: ignore[index]


def test_config_version_is_stamped(config: TenantConfig) -> None:
    assert config.config_version == "tenant-cfg-default-3"


# --- weights ---------------------------------------------------------------


def test_weights_cover_all_five_components(config: TenantConfig) -> None:
    assert set(config.weights) == set(ComponentName)


def test_weights_sum_to_one_hundred(config: TenantConfig) -> None:
    # The denominator is computed, not assumed -- but "out of 100" is the
    # reading every human applies, and this keeps it true.
    assert sum(config.weights.values()) == 100


def test_weights_are_the_rubric_values(config: TenantConfig) -> None:
    assert config.weights[ComponentName.WHAT_HAPPENED] == 25
    assert config.weights[ComponentName.CLIENT_SAID] == 20
    assert config.weights[ComponentName.NEXT_STEP_DATE] == 25
    assert config.weights[ComponentName.DEAL_SPECIFICS] == 20
    assert config.weights[ComponentName.CLARITY] == 10


# --- band boundaries -------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "expected"),
    [
        (0, Band.POOR),
        (39, Band.POOR),  # boundary
        (40, Band.FAIR),
        (69, Band.FAIR),  # boundary
        (70, Band.GOOD),
        (84, Band.GOOD),  # boundary
        (85, Band.EXCELLENT),
        (100, Band.EXCELLENT),
    ],
)
def test_band_for_splits_at_39_69_and_84(
    config: TenantConfig, total: int, expected: Band
) -> None:
    assert config.band_for(total) is expected


def test_band_for_is_total_over_the_top_bound_safe(config: TenantConfig) -> None:
    # Not reachable from compute (which bounds the total), but band_for is a
    # labelling function, not a validator: it labels rather than raising.
    assert config.band_for(101) is Band.EXCELLENT


def test_band_boundaries_are_ascending(config: TenantConfig) -> None:
    uppers = [upper for _, upper in config.band_boundaries]
    assert uppers == sorted(uppers)
    assert uppers[-1] == 100


# --- thresholds ------------------------------------------------------------


def test_thresholds_are_the_rubric_values(config: TenantConfig) -> None:
    assert config.accept_threshold == 70
    assert config.flag_threshold == 40


def test_accept_threshold_sits_above_the_flag_threshold(config: TenantConfig) -> None:
    assert config.flag_threshold < config.accept_threshold


# --- per-type suppression --------------------------------------------------


def test_no_contact_suppresses_client_said_and_deal_specifics(
    config: TenantConfig,
) -> None:
    # A "called, no answer" note cannot report what the client said. Scoring
    # those 0 instead of removing them would cap an honest note at 60/100.
    assert config.suppressed_components_by_type[NoteType.NO_CONTACT] == frozenset(
        {ComponentName.CLIENT_SAID, ComponentName.DEAL_SPECIFICS}
    )


def test_only_no_contact_suppresses_components_by_type(config: TenantConfig) -> None:
    assert set(config.suppressed_components_by_type) == {NoteType.NO_CONTACT}


def test_no_contact_may_only_be_asked_for_the_next_attempt_date(
    config: TenantConfig,
) -> None:
    """Register item 130: what happened is not a question a no-contact note can owe."""
    assert config.allowed_missing_by_type[NoteType.NO_CONTACT] == frozenset(
        {MissingComponent.NEXT_STEP_WITH_DATE}
    )


def test_the_other_five_human_types_allow_all_three(config: TenantConfig) -> None:
    for note_type in (
        NoteType.CALLBACK,
        NoteType.DISCOVERY,
        NoteType.VIEWING,
        NoteType.NEGOTIATION,
        NoteType.WON_LOST,
    ):
        assert config.allowed_missing_by_type[note_type] == frozenset(MissingComponent)


def test_system_event_has_no_missing_entry(config: TenantConfig) -> None:
    # It is never scored and never reaches the vagueness pass, so an entry
    # would describe a path that does not exist.
    assert NoteType.SYSTEM_EVENT not in config.allowed_missing_by_type


# --- ASSUMPTION[Q13] -------------------------------------------------------


def test_deal_specifics_is_switched_off_pending_q13(config: TenantConfig) -> None:
    # No lead field is CONFIRMED to carry the business line, and inventing one
    # is not allowed -- so the component leaves the denominator (100 -> 80)
    # rather than being marked against a checklist we cannot choose.
    assert config.business_line_field is None
    assert config.deal_specifics_applicable is False


def test_deal_specifics_still_has_a_weight_to_restore(config: TenantConfig) -> None:
    # The weight stays in the table so flipping the flag is a one-line change
    # and the pre-Q13 denominator is derivable.
    assert config.weights[ComponentName.DEAL_SPECIFICS] == 20
    assert (
        sum(config.weights.values()) - config.weights[ComponentName.DEAL_SPECIFICS]
        == 80
    )


# --- thin evidence, caps, TTLs ---------------------------------------------


def test_thin_evidence_bounds(config: TenantConfig) -> None:
    assert config.min_note_chars == 15
    assert config.min_note_tokens == 3


def test_caps_and_windows(config: TenantConfig) -> None:
    assert config.clarification_cap == 1
    assert config.rate_limit_per_hour == 3
    assert config.rate_limit_per_day == 10
    assert config.rate_limit_window_seconds == 3600


def test_ttls(config: TenantConfig) -> None:
    assert config.attempt_ttl_seconds == 21600  # 6h
    assert config.idempotency_ttl_seconds == 86400  # 24h


def test_enforcement_mode_is_advisory_by_default(config: TenantConfig) -> None:
    # Carried, never branched on: enforcement is the CRM's.
    assert config.enforcement_mode is EnforcementMode.ADVISORY
    assert [m.value for m in EnforcementMode] == ["advisory", "blocking"]


def test_every_frozenset_str_field_is_stored_casefolded() -> None:
    """Each frozenset[str] table on TenantConfig is folded, so a new one must join the fold."""
    import typing

    hints = typing.get_type_hints(TenantConfig)
    tables = [name for name, hint in hints.items() if hint == frozenset[str]]
    assert tables, "no frozenset[str] field found"
    mixed = dataclasses.replace(
        get_tenant_config("tenant-a"),
        **{name: frozenset({"AbC", "Not INTERESTED"}) for name in tables},
    )
    for name in tables:
        table = getattr(mixed, name)
        assert table == frozenset(word.casefold() for word in table), name


def _broken(config: TenantConfig, **changes: object) -> TenantConfig:
    return dataclasses.replace(config, **changes)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "checks_by_component",
            MappingProxyType(
                {
                    c: v
                    for c, v in get_tenant_config(
                        "tenant-a"
                    ).checks_by_component.items()
                    if c is not ComponentName.CLARITY
                }
            ),
            "checks_by_component",
        ),
        (
            "marks_by_true_count",
            MappingProxyType(
                {
                    c: v
                    for c, v in get_tenant_config(
                        "tenant-a"
                    ).marks_by_true_count.items()
                    if c is not ComponentName.CLARITY
                }
            ),
            "marks_by_true_count",
        ),
        (
            "marks_by_true_count",
            MappingProxyType(
                {
                    **get_tenant_config("tenant-a").marks_by_true_count,
                    ComponentName.CLARITY: (0, 10),
                }
            ),
            "marks_length",
        ),
        (
            "marks_by_true_count",
            MappingProxyType(
                {
                    **get_tenant_config("tenant-a").marks_by_true_count,
                    ComponentName.CLARITY: (0, 10, 5),
                }
            ),
            "marks_order",
        ),
        (
            "marks_by_true_count",
            MappingProxyType(
                {
                    **get_tenant_config("tenant-a").marks_by_true_count,
                    ComponentName.CLARITY: (1, 5, 10),
                }
            ),
            "marks_order",
        ),
        (
            "checks_by_component",
            MappingProxyType(
                {
                    **get_tenant_config("tenant-a").checks_by_component,
                    ComponentName.WHAT_HAPPENED: (
                        CheckName.WH_OUTCOME,
                        CheckName.CL_READABLE,
                    ),
                }
            ),
            "checks_coverage",
        ),
    ],
    ids=[
        "checks-missing-component",
        "marks-missing-component",
        "marks-length",
        "marks-descending",
        "marks-not-from-zero",
        "check-listed-twice",
    ],
)
def test_check_refuses_a_rubric_whose_checks_and_marks_do_not_line_up(
    config: TenantConfig, field: str, value: object, reason: str
) -> None:
    """Register item 131: each mark-table invariant `_check` holds, refused by name."""
    with pytest.raises(ValueError, match=f"^{reason}$"):
        _check(_broken(config, **{field: value}))
