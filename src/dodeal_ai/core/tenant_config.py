"""Per-tenant configuration files (register item 97, moved to core by F2).

With DODEAL_TENANT_CONFIG_DIR set, each `<tenant>.json` there is ONE object with
a section per unit, e.g. {"unit_a": {...}}. At startup every section a unit has
registered a parser for is validated by that parser; a section nobody parses is
ignored, and a missing section leaves that unit on its own default.

Core knows no unit's schema: the lifespan hands in the parsers, and a unit reads
back only its own parsed section. An invalid file refuses startup naming the
tenant, never the path and never a value, and nothing is installed. A section
parser's own failure -- whatever exception it raises, not only ValueError --
names the tenant AND the section (register item 128), so a broken unit_a file
never reads as an unnamed crash.

RUNTIME OVERRIDES (register item 97, second half) live below the file loader:
a section set through the admin route, stored in db2, resolved before the file
and cached per process. See `resolve_section`. One record holds every section
set at runtime, each with its own stamps: a PUT merges its section in (register
item 193), so one unit's change never drops or re-stamps another's.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import redis

from dodeal_ai.core.auth.claims import normalise_tenant_label
from dodeal_ai.core.breaker import breaker_field, operational_breaker
from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.redis import get_operational_client

_logger = logging.getLogger("dodeal_ai.tenant_config")

# A unit's section parser: the raw JSON value in, the unit's config out. It
# raises ValueError (pydantic's ValidationError is one) for anything invalid.
type SectionParser = Callable[[object], object]

# A unit's answer to "does this body mark notes as the section in force does?"
# (register item 97): the body and the parsed section in force (None for the
# default) in; that section's config_version when it does, None when not.
type VersionKeeper = Callable[[dict, object | None], str | None]

# Tenant -> section name -> that unit's parsed config, filled once at startup.
_LOADED: dict[str, dict[str, object]] = {}


def load_tenant_configs(directory: Path, parsers: Mapping[str, SectionParser]) -> None:
    """Validate every `<tenant>.json` in `directory` and install them all.

    All or nothing: the first invalid file raises ConfigError naming its tenant
    and nothing is installed. Read at startup only, so no request reads disk.
    """
    if not directory.is_dir():
        raise ConfigError("tenant_config_dir_unreadable")
    loaded: dict[str, dict[str, object]] = {}
    for path in sorted(directory.glob("*.json")):
        tenant = path.stem
        if normalise_tenant_label(tenant) != tenant:
            raise ConfigError("tenant_config_invalid_name")
        invalid = ConfigError(f"tenant_config_invalid:{tenant}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise invalid from None
        if not isinstance(raw, dict):
            raise invalid
        parsed: dict[str, object] = {}
        for name, parse in parsers.items():
            if name not in raw:
                continue
            try:
                parsed[name] = parse(raw[name])
            except Exception:  # noqa: BLE001 - register item 128: any parser failure
                # A section parser's error can be anything it likes (pydantic's
                # ValidationError included). `from None`: whatever it raised can
                # quote the rejected value, and never the path, so it must not
                # chain into this one either.
                raise ConfigError(f"tenant_config_invalid:{tenant}:{name}") from None
        loaded[tenant] = parsed
    _LOADED.clear()
    _LOADED.update(loaded)


def tenant_section(tenant: str, section: str) -> object | None:
    """This tenant's parsed `section`, or None when it has no file or no section."""
    return _LOADED.get(tenant, {}).get(section)


def clear_tenant_configs() -> None:
    """Every tenant back to every unit's default: the shutdown half of the load.
    The override cache and the registered parsers go with the file sections."""
    _LOADED.clear()
    _CACHE.clear()
    _PARSERS.clear()


# --- runtime overrides (register item 97) ----------------------------------
#
# An administrator changes a tenant's rules without a release: the CRM PUTs a
# section, it is validated by the SAME parser the tenant file uses, stamped with
# TWO versions and stored in db2. The dated policy_version moves on every PUT;
# the config_version moves only when the unit's keeper says a mark would, so a
# stored judgement's config_version still groups the judgements that mark
# alike. Every judgement then resolves its rules in
# the one order below -- override, else the startup file, else the unit's
# default -- through a per-process cache, and fails SAFE: a store it cannot
# reach falls back to the last value it saw, then the file, then the default,
# and the defaults are the safe rules (advisory, blocking off, rep numbers off).

# Section name -> the unit parser that validates it, registered by the lifespan
# so the override store can parse a section core knows nothing about.
_PARSERS: dict[str, SectionParser] = {}

# How many past versions the history key keeps, newest first.
HISTORY_LIMIT = 50

# How many times a PUT retries its compare-and-set when another PUT for the same
# tenant landed between its read and its write. Beyond it, 409.
_WRITE_ATTEMPTS = 3


def override_key(tenant: str) -> str:
    """The version in force. NO TTL, deliberately: a rule an administrator set
    must not silently lapse back to the file on a timer."""
    return f"tenant_cfg:{tenant}"


def override_history_key(tenant: str) -> str:
    """Past versions, newest first, capped at HISTORY_LIMIT. NO TTL, for the same
    reason: it is the record of which rules a stored judgement was made under."""
    return f"tenant_cfg_history:{tenant}"


# KEYS: the version key, the history key. ARGV: the value read before, the new
# record, the history cap. Writes only if the version key still holds what was
# read, so two PUTs cannot both claim one dated version.
_SET_OVERRIDE_SCRIPT = """
local current = redis.call('GET', KEYS[1]) or ''
if current ~= ARGV[1] then
  return 0
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('LPUSH', KEYS[2], ARGV[2])
redis.call('LTRIM', KEYS[2], 0, tonumber(ARGV[3]) - 1)
return 1
"""


class InvalidOverride(Exception):
    """A section that failed its parser, or a body that is not one. Fixed text."""

    def __init__(self) -> None:
        super().__init__("invalid_tenant_config")


class OverrideUnavailable(Exception):
    """The override store could not be read or written. Fixed text."""

    def __init__(self) -> None:
        super().__init__("tenant_config_unavailable")


class OverrideConflict(Exception):
    """Other PUTs kept landing between this one's read and write. Fixed text."""

    def __init__(self) -> None:
        super().__init__("tenant_config_conflict")


@dataclass(frozen=True, slots=True)
class SectionStamp:
    """One section's own stamps (register item 193): the config_version it is
    judged under, when it was last PUT, and that PUT's policy_version."""

    version: str
    set_at: str
    policy_version: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "version": self.version,
            "set_at": self.set_at,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True, slots=True)
class OverrideRecord:
    """One stored override: the LAST PUT's config_version, set_at and dated
    policy_version (None on a record stored before register item 97 gave it
    one), every section in force, and each section's own stamps (item 193).

    A record stored before item 193 has no `stamps`: it held one section, so
    the top-level stamps are that section's."""

    version: str
    set_at: str
    sections: dict[str, object]
    policy_version: str | None = None
    stamps: dict[str, SectionStamp] = field(default_factory=dict)

    def stamp_for(self, section: str) -> SectionStamp:
        """`section`'s own stamps, else the top-level ones (an older record)."""
        stamp = self.stamps.get(section)
        if stamp is not None:
            return stamp
        return SectionStamp(self.version, self.set_at, self.policy_version)

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "policy_version": self.policy_version,
                "set_at": self.set_at,
                "sections": self.sections,
                "stamps": {
                    name: stamp.to_dict() for name, stamp in self.stamps.items()
                },
            },
            sort_keys=True,
        )


@dataclass(frozen=True, slots=True)
class ResolvedSection:
    """What a tenant's section resolved to, and from where. `value` is None when
    the unit's own default applies; `policy_version` only an override has."""

    value: object | None
    version: str | None
    set_at: str | None
    source: Literal["override", "file", "default"]
    policy_version: str | None = None


@dataclass(frozen=True, slots=True)
class _Entry:
    fetched_at: float
    raw: str | None
    record: OverrideRecord | None
    parsed: dict[str, object]


# Tenant -> the last override read for it, with when it was read (monotonic).
_CACHE: dict[str, _Entry] = {}


def register_section_parsers(parsers: Mapping[str, SectionParser]) -> None:
    """The lifespan's registration of every unit's section parser."""
    _PARSERS.clear()
    _PARSERS.update(parsers)


def reset_override_cache() -> None:
    """Forget every cached override: the next resolve reads the store."""
    _CACHE.clear()


def _record(tenant: str, raw: str | None) -> OverrideRecord | None:
    """The stored record's shape, or None. A record that is not one is logged
    by tenant and ignored -- never its content."""
    if raw is None:
        return None
    try:
        document = json.loads(raw)
        version, set_at = str(document["version"]), str(document["set_at"])
        sections = dict(document["sections"])
        # Indexed above, so a dict here: .get cannot meet a list or a string.
        policy_version = document.get("policy_version")
        return OverrideRecord(
            version=version,
            set_at=set_at,
            sections=sections,
            policy_version=_optional(policy_version),
            stamps={
                str(name): SectionStamp(
                    version=str(stamp["version"]),
                    set_at=str(stamp["set_at"]),
                    policy_version=_optional(stamp.get("policy_version")),
                )
                for name, stamp in dict(document.get("stamps") or {}).items()
            },
        )
    except (ValueError, KeyError, TypeError, AttributeError):
        _invalid(tenant)
        return None


def _optional(value: object) -> str | None:
    return None if value is None else str(value)


def _invalid(tenant: str) -> None:
    _logger.warning(
        "tenant_config_override_invalid",
        extra={"reason_code": "tenant_config_override_invalid", "tenant": tenant},
    )


def _decode(tenant: str, raw: str | None) -> tuple[OverrideRecord | None, dict]:
    """The stored record and each registered section parsed, or (None, {}). A
    section today's parser refuses voids the record: it is logged and ignored."""
    record = _record(tenant, raw)
    if record is None:
        return None, {}
    try:
        parsed = {
            name: _PARSERS[name](section)
            for name, section in record.sections.items()
            if name in _PARSERS
        }
    except Exception:  # noqa: BLE001 - register item 128: any parser failure
        _invalid(tenant)
        return None, {}
    return record, parsed


async def _read(tenant: str) -> str | None:
    raw = await operational_breaker().call(
        lambda: get_operational_client().get(override_key(tenant))
    )
    assert raw is None or isinstance(raw, str)  # decode_responses=True
    return raw


async def _entry(tenant: str, *, fresh: bool = False) -> _Entry | None:
    """The cached override, re-read once it is older than the cache time, or
    at once when `fresh`. On a store failure, the last one read -- however
    old -- or None; when `fresh`, the failure is raised instead."""
    cached = _CACHE.get(tenant)
    now = time.monotonic()
    if (
        not fresh
        and cached is not None
        and now - cached.fetched_at < get_settings().tenant_config_cache_seconds
    ):
        return cached
    try:
        raw = await _read(tenant)
    except redis.RedisError as exc:
        _logger.warning(
            "tenant_config_override_unavailable",
            extra={
                "reason_code": "tenant_config_override_unavailable",
                "tenant": tenant,
                **breaker_field(exc),
            },
        )
        if fresh:
            raise
        return cached
    record, parsed = _decode(tenant, raw)
    entry = _Entry(fetched_at=now, raw=raw, record=record, parsed=parsed)
    _CACHE[tenant] = entry
    return entry


async def resolve_section(
    tenant: str, section: str, *, fresh: bool = False
) -> ResolvedSection:
    """THE resolution order for one tenant's section: the runtime override,
    else the startup file, else the unit's default (value None). `fresh`
    reads the override past the cache and raises RedisError if it cannot."""
    entry = await _entry(tenant, fresh=fresh)
    if entry is not None and entry.record is not None and section in entry.parsed:
        # This section's own stamps (item 193): another section's PUT never
        # moves them.
        stamp = entry.record.stamp_for(section)
        return ResolvedSection(
            value=entry.parsed[section],
            version=stamp.version,
            set_at=stamp.set_at,
            source="override",
            policy_version=stamp.policy_version,
        )
    from_file = tenant_section(tenant, section)
    if from_file is not None:
        return ResolvedSection(from_file, None, None, "file")
    return ResolvedSection(None, None, None, "default")


def _next_version(
    kind: str, tenant: str, current: Iterable[str | None], today: str
) -> str:
    """tenant-<kind>-<tenant>-<YYYYMMDD>-<n>, n one past the highest of that
    day's versions of this kind among `current`, the stamps in force. Across
    every section (item 193), so two sections never share one stamp."""
    prefix = f"tenant-{kind}-{tenant}-{today}-"
    counts = [
        int(version.removeprefix(prefix))
        for version in current
        if version is not None and version.startswith(prefix)
    ]
    return f"{prefix}{max(counts, default=0) + 1}"


def _merged(
    current: OverrideRecord | None, section: str, stamped: dict, stamp: SectionStamp
) -> OverrideRecord:
    """The stored record with `section` replaced and every other section, and
    its stamps, carried over unchanged (register item 193). An older record's
    one section gets its top-level stamps written down as its own first."""
    sections = dict(current.sections) if current else {}
    stamps = {name: current.stamp_for(name) for name in sections} if current else {}
    sections[section] = stamped
    stamps[section] = stamp
    return OverrideRecord(
        version=stamp.version,
        set_at=stamp.set_at,
        sections=sections,
        policy_version=stamp.policy_version,
        stamps=stamps,
    )


async def set_override(
    tenant: str,
    section: str,
    body: object,
    *,
    now: datetime,
    keep_version: VersionKeeper,
) -> OverrideRecord:
    """Validate `body` with the section's parser and store it as the version in
    force, with two stamps (register item 97): the next dated policy_version,
    always, and as its config_version the one in force when `keep_version` says
    the body marks alike, else the next dated one.

    MERGED, never replaced (register item 193): the section goes into the
    stored record beside every other section, under the same compare-and-set,
    and the other sections keep their own stamps.

    InvalidOverride for a body its parser refuses (nothing is written);
    OverrideUnavailable when the store cannot be read or written;
    OverrideConflict when concurrent PUTs keep winning the race.
    """
    if not isinstance(body, dict):
        raise InvalidOverride()
    parser = _PARSERS[section]
    today = now.astimezone(UTC).strftime("%Y%m%d")
    for _ in range(_WRITE_ATTEMPTS):
        try:
            raw = await _read(tenant)
        except redis.RedisError:
            raise OverrideUnavailable() from None
        current, current_parsed = _decode(tenant, raw)
        # The section a judgement made now would use: override, else file.
        in_force = current_parsed.get(section, tenant_section(tenant, section))
        in_force_stamps = (
            [current.version, *(current.stamp_for(n).version for n in current.sections)]
            if current
            else []
        )
        try:
            kept = keep_version(body, in_force)
            version = (
                kept
                if kept is not None
                else _next_version("cfg", tenant, in_force_stamps, today)
            )
            stamped = {**body, "config_version": version}
            parsed = parser(stamped)
        except Exception:  # noqa: BLE001 - any parser failure is an invalid body
            raise InvalidOverride() from None
        record = _merged(
            current,
            section,
            stamped,
            SectionStamp(
                version=version,
                set_at=now.astimezone(UTC).isoformat(),
                policy_version=_next_version(
                    "policy",
                    tenant,
                    [current.policy_version] if current else [],
                    today,
                ),
            ),
        )
        record_json = record.to_json()
        try:
            written = await _write(tenant, raw or "", record_json)
        except redis.RedisError:
            raise OverrideUnavailable() from None
        if written:
            _CACHE[tenant] = _Entry(
                fetched_at=time.monotonic(),
                raw=record_json,
                record=record,
                parsed={**current_parsed, section: parsed},
            )
            _logger.info(
                "tenant_config_changed",
                extra={
                    "tenant": tenant,
                    "section": section,
                    "version": version,
                    "policy_version": record.policy_version,
                },
            )
            return record
    raise OverrideConflict()


async def _write(tenant: str, expected: str, record_json: str) -> bool:
    """One compare-and-set of the version in force and its history."""
    written = await operational_breaker().call(
        lambda: get_operational_client().eval(
            _SET_OVERRIDE_SCRIPT,
            2,
            override_key(tenant),
            override_history_key(tenant),
            expected,
            record_json,
            HISTORY_LIMIT,
        )
    )
    return int(written) == 1


async def override_history(tenant: str) -> list[OverrideRecord]:
    """Past versions, newest first, at most HISTORY_LIMIT. A record that no
    longer decodes is skipped rather than served."""
    try:
        raws = await operational_breaker().call(
            lambda: get_operational_client().lrange(
                override_history_key(tenant), 0, HISTORY_LIMIT - 1
            )
        )
    except redis.RedisError:
        raise OverrideUnavailable() from None
    records = []
    for raw in raws:
        assert isinstance(raw, str)  # decode_responses=True
        record = _record(tenant, raw)
        if record is not None:
            records.append(record)
    return records
