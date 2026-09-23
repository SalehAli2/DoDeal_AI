"""A runtime PUT merges its section into the stored record (register item 193):
every other section, and its stamps, survive under the same compare-and-set."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from dodeal_ai.core import tenant_config
from dodeal_ai.core.tenant_config import (
    InvalidOverride,
    override_key,
    register_section_parsers,
    resolve_section,
    set_override,
)
from dodeal_ai.main import TENANT_CONFIG_SECTIONS
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    kept_config_version,
)
from tests.conftest import RedisFakes

NOW = datetime(2026, 9, 23, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
CFG = "tenant-cfg-tenant-a-20260923-"
POLICY = "tenant-policy-tenant-a-20260923-"
OTHER = "unit_b"
DEFAULT_A = "tenant-cfg-default-4"


def _parse_other(raw: object) -> dict:
    """A stand-in second section: an object with a `limit`."""
    if not isinstance(raw, dict) or "limit" not in raw:
        raise ValueError("other")
    return dict(raw)


@pytest.fixture(autouse=True)
def _two_sections() -> Iterator[None]:
    register_section_parsers({**TENANT_CONFIG_SECTIONS, OTHER: _parse_other})
    yield
    register_section_parsers(TENANT_CONFIG_SECTIONS)


async def _put_a(body: dict, now: datetime = NOW) -> tenant_config.OverrideRecord:
    return await set_override(
        "tenant-a", UNIT_A_SECTION, body, now=now, keep_version=kept_config_version
    )


async def _put_other(body: dict, now: datetime = LATER) -> tenant_config.OverrideRecord:
    return await set_override(
        "tenant-a", OTHER, body, now=now, keep_version=lambda _body, _in: None
    )


async def _resolved(section: str) -> tuple:
    tenant_config.reset_override_cache()
    resolved = await resolve_section("tenant-a", section)
    return resolved.version, resolved.policy_version, resolved.set_at


async def test_a_unit_b_put_keeps_unit_a_and_its_versions() -> None:
    """The guard: unit_a's section, config_version, policy_version and set_at
    read the same after a unit_b PUT as before it."""
    await _put_a({"accept_threshold": 77})
    before = await _resolved(UNIT_A_SECTION)

    record = await _put_other({"limit": 5})

    assert record.sections[UNIT_A_SECTION]["accept_threshold"] == 77
    assert await _resolved(UNIT_A_SECTION) == before
    assert before == (DEFAULT_A, f"{POLICY}1", NOW.isoformat())
    assert await _resolved(OTHER) == (f"{CFG}1", f"{POLICY}2", LATER.isoformat())


async def test_a_unit_a_put_keeps_the_other_section() -> None:
    """The merge runs both ways: unit_a's PUT carries the other section over."""
    await _put_other({"limit": 5})
    other = await _resolved(OTHER)

    record = await _put_a({"accept_threshold": 77}, now=LATER)

    assert record.sections[OTHER] == {"limit": 5, "config_version": f"{CFG}1"}
    assert await _resolved(OTHER) == other


async def test_a_record_from_before_the_merge_keeps_its_one_sections_stamps(
    redis_fakes: RedisFakes,
) -> None:
    """A record with no per-section stamps gives its section the top-level ones."""
    redis_fakes.operational.store[override_key("tenant-a")] = json.dumps(
        {
            "version": f"{CFG}3",
            "policy_version": f"{POLICY}4",
            "set_at": "t",
            "sections": {UNIT_A_SECTION: {"config_version": f"{CFG}3"}},
        }
    )
    assert await _resolved(UNIT_A_SECTION) == (f"{CFG}3", f"{POLICY}4", "t")

    await _put_other({"limit": 5})

    assert await _resolved(UNIT_A_SECTION) == (f"{CFG}3", f"{POLICY}4", "t")
    assert await _resolved(OTHER) == (f"{CFG}4", f"{POLICY}5", LATER.isoformat())


async def test_no_two_sections_share_a_dated_stamp() -> None:
    """Both counters run across the whole record, never per section."""
    await _put_a({"weights": _weights(what_happened=30, client_said=15)})
    await _put_other({"limit": 5})
    third = await _put_a({"weights": _weights(clarity=15, client_said=15)})

    assert (third.version, third.policy_version) == (f"{CFG}3", f"{POLICY}3")
    assert (await _resolved(OTHER))[:2] == (f"{CFG}2", f"{POLICY}2")


async def test_a_lost_race_rereads_and_keeps_the_section_that_won(
    monkeypatch, redis_fakes: RedisFakes
) -> None:
    """A PUT that loses the compare-and-set to another section's PUT merges
    into the record that won, not into the one it first read."""
    real_write = tenant_config._write
    raced = False

    async def _write_after_a_rival(tenant: str, expected: str, record: str) -> bool:
        nonlocal raced
        if not raced:
            raced = True
            await _put_a({"accept_threshold": 71})
        return await real_write(tenant, expected, record)

    monkeypatch.setattr(tenant_config, "_write", _write_after_a_rival)
    record = await _put_other({"limit": 9})

    assert record.sections[UNIT_A_SECTION]["accept_threshold"] == 71
    assert record.sections[OTHER]["limit"] == 9
    stored = json.loads(redis_fakes.operational.store[override_key("tenant-a")])
    assert set(stored["sections"]) == {UNIT_A_SECTION, OTHER}


async def test_the_cache_after_a_put_holds_every_section(
    redis_fakes: RedisFakes,
) -> None:
    """The writer's own cache resolves both sections without reading db2."""
    await _put_a({"accept_threshold": 77})
    await _put_other({"limit": 5})
    redis_fakes.operational.raise_on.add("get")

    assert (await resolve_section("tenant-a", UNIT_A_SECTION)).source == "override"
    assert (await resolve_section("tenant-a", OTHER)).value == {
        "limit": 5,
        "config_version": f"{CFG}1",
    }


async def test_a_refused_section_writes_nothing(redis_fakes: RedisFakes) -> None:
    """The other section's parser refusing leaves unit_a's record untouched."""
    await _put_a({"accept_threshold": 77})
    stored = redis_fakes.operational.store[override_key("tenant-a")]

    with pytest.raises(InvalidOverride):
        await _put_other({"no_limit": 1})

    assert redis_fakes.operational.store[override_key("tenant-a")] == stored


async def test_a_record_with_a_broken_stamp_is_ignored(
    redis_fakes: RedisFakes,
) -> None:
    """A stamps entry that is not an object voids the record, as any bad shape."""
    redis_fakes.operational.store[override_key("tenant-a")] = json.dumps(
        {
            "version": "v",
            "set_at": "t",
            "sections": {UNIT_A_SECTION: {"config_version": "v"}},
            "stamps": {UNIT_A_SECTION: ["v"]},
        }
    )
    assert (await resolve_section("tenant-a", UNIT_A_SECTION)).source == "default"


def _weights(**changes: int) -> dict[str, int]:
    weights = {
        "what_happened": 25,
        "client_said": 20,
        "next_step_date": 25,
        "deal_specifics": 20,
        "clarity": 10,
    }
    weights.update(changes)
    return weights
