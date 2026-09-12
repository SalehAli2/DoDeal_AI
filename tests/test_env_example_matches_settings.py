"""`.env.example` documents every setting, and only real ones.

WHY THIS EXISTS. `.env.example` is the file a new deployment copies, and it is
the only place most people ever learn a setting exists. It had drifted:
`DODEAL_LLM_PROFILES` entered `Settings` in Piece M (`b263c8b`) and never
reached the example, because a session may not write that file (CLAUDE.md) and
the row it reported to the lead was never pasted. Nothing failed, because
nothing checked -- the ledger's claim that the file was "audited field-by-field
against core/config.py::Settings" was true when it was written and silently
stopped being true the next time a field was added.

A hand audit cannot hold that invariant; only a test run on every commit can.

WHAT THIS DELIBERATELY NEVER DOES: reveal a VALUE. The drift tests compare KEY
NAMES only. Exactly one test reads values -- the secret-shape check at the
bottom -- and it compares without printing, naming only the key that failed. A
guard that leaked the contents of the file it guards would be a poor trade.

That one test is also what makes this file COMMITTABLE BY A SESSION that cannot
read it: the permission rule denies `.env.*`, so no session can eyeball the
paste, and "every secret-shaped row holds a placeholder" is the assertion that
replaces eyeballing it.

A commented row (`# DODEAL_LLM_BASE_URL=`) COUNTS AS DOCUMENTED. That is the
shape an optional override takes: present so it is discoverable, commented so
it stays unset unless somebody means it.
"""

from __future__ import annotations

import pathlib
import re

from dodeal_ai.core.config import Settings

_EXAMPLE = pathlib.Path(".env.example")

# Rows that are deliberately NOT Settings fields, each with the reason it is
# exempt. An allowlist rather than a looser rule: a key earns its place here by
# being named, so the next stale row still fails instead of slipping through.
_NOT_SETTINGS: dict[str, str] = {
    # Read by tests/redis_real/conftest.py to point the hand-run lane at a real
    # server. The service never reads it -- it is lane configuration, and it is
    # in the example so somebody running the lane knows the name.
    "DODEAL_REDIS_REAL_URL": "redis_real test lane, never the service",
}

# A row, set or commented out. The name is all that is captured -- the pattern
# stops at the `=` so no value is ever pulled into a variable, let alone a
# failure message.
_ROW = re.compile(r"^\s*#?\s*(DODEAL_[A-Z0-9_]+)\s*=", re.MULTILINE)

# The same row, with its value, for the secret-shape check below. Used by ONE
# test, which compares and never prints -- see that test's docstring.
_ROW_WITH_VALUE = re.compile(r"^\s*#?\s*(DODEAL_[A-Z0-9_]+)\s*=(.*)$", re.MULTILINE)

# A name that promises a credential. Matched on the NAME, so a new secret-shaped
# setting is covered the day it is added rather than when somebody remembers.
_SECRET_NAME = re.compile(r"_(KEY|KEYS|SECRET|TOKEN|PASSWORD)$")

# Every placeholder in this repo says "change-me"; a JSON map of placeholders
# contains it too. No real credential from any provider contains this string.
_PLACEHOLDER_MARK = "change-me"


def _documented_keys() -> set[str]:
    return set(_ROW.findall(_EXAMPLE.read_text(encoding="utf-8")))


def _settings_keys() -> set[str]:
    return {f"DODEAL_{name.upper()}" for name in Settings.model_fields}


def test_the_example_file_exists_and_has_rows() -> None:
    # Fail closed on a rename or an empty file: an example nobody can read would
    # make both tests below pass by comparing against an empty set.
    assert _EXAMPLE.is_file(), f"{_EXAMPLE} is missing"
    assert len(_documented_keys()) > 20, "too few DODEAL_* rows to be the real file"


def test_every_setting_is_documented() -> None:
    """A field in Settings that nobody can discover is a field nobody sets."""
    missing = sorted(_settings_keys() - _documented_keys())
    assert not missing, (
        "Settings fields with no row in .env.example -- add each with its "
        "three-line comment:\n  " + "\n  ".join(missing)
    )


def test_the_exemptions_are_all_still_needed() -> None:
    """An exemption for a key nobody documents any more is dead weight, and the
    next person reads it as a rule rather than as a leftover."""
    unused = sorted(set(_NOT_SETTINGS) - _documented_keys())
    assert not unused, "exempted keys no longer in .env.example:\n  " + "\n  ".join(
        unused
    )


def test_no_row_documents_a_setting_that_no_longer_exists() -> None:
    """A stale row is worse than a missing one: it reads as supported."""
    stale = sorted(_documented_keys() - _settings_keys() - set(_NOT_SETTINGS))
    assert not stale, (
        "rows in .env.example with no matching Settings field -- remove each, "
        "or restore the field:\n  " + "\n  ".join(stale)
    )


def test_no_secret_shaped_row_holds_a_real_looking_value() -> None:
    """The example is copied into a real `.env`; a real key pasted here is a
    committed credential.

    THE ONE TEST THAT LOOKS AT A VALUE, and it never reveals one: it asserts the
    value is a placeholder and reports only the KEY that failed. That is what
    makes this file safe to commit without reading it by hand -- which is the
    situation every session is in, since the permission rule denies `.env.*`.
    """
    offenders = sorted(
        name
        for name, value in _ROW_WITH_VALUE.findall(_EXAMPLE.read_text(encoding="utf-8"))
        if _SECRET_NAME.search(name)
        and value.strip()
        and _PLACEHOLDER_MARK not in value
    )
    assert not offenders, (
        "secret-shaped rows in .env.example whose value is not a placeholder "
        f"(values deliberately not shown; expected to contain "
        f"{_PLACEHOLDER_MARK!r}):\n  " + "\n  ".join(offenders)
    )
