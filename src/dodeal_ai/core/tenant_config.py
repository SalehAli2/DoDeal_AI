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
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path

from dodeal_ai.core.auth.claims import normalise_tenant_label
from dodeal_ai.core.config import ConfigError

# A unit's section parser: the raw JSON value in, the unit's config out. It
# raises ValueError (pydantic's ValidationError is one) for anything invalid.
type SectionParser = Callable[[object], object]

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
    """Every tenant back to every unit's default: the shutdown half of the load."""
    _LOADED.clear()
