"""Model profiles: resolution, the fallback rule, and the ceiling rule.

Piece M / report R17. The seam kept one method and one parameterless factory;
what changed is that a caller now NAMES its task and the profile table decides
what that task runs on. Everything here is about that table -- nothing in this
file makes a model call.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

from dodeal_ai.core.config import ConfigError, LLMProvider, Settings, _build_settings
from dodeal_ai.core.llm import LLMConfigurationError
from dodeal_ai.core.llm import profiles as profiles_module
from dodeal_ai.core.llm.profiles import (
    KNOWN_PROFILES,
    PROFILE_UNIT_A_CLASSIFY,
    PROFILE_UNIT_A_SCORE,
    PROFILE_UNIT_A_VAGUE,
    ResolvedProfile,
    resolve_profile,
)

FALLBACK_MODEL = "fallback-pinned-2026-01-01"
PROFILE_MODEL = "profile-pinned-2026-01-01"


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    """Settings built from env with no .env file in play, so a stray local file
    cannot supply a profile and mask the fallback path."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-only-key")
    for key in ("PROVIDER", "MODEL", "PROFILES"):
        monkeypatch.delenv(f"DODEAL_LLM_{key}", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(f"DODEAL_LLM_{key}", value)
    return _build_settings(_env_file=None)


def _profiles_json(name: str, **fields: object) -> str:
    return json.dumps({name: {"provider": "anthropic", **fields}})


# --- a configured profile wins ---------------------------------------------


def test_a_configured_profile_resolves_to_its_own_provider_model_temperature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(
        monkeypatch,
        PROVIDER="anthropic",
        MODEL=FALLBACK_MODEL,
        PROFILES=_profiles_json(
            PROFILE_UNIT_A_VAGUE, model=PROFILE_MODEL, temperature=0.4
        ),
    )
    resolved = resolve_profile(settings, PROFILE_UNIT_A_VAGUE)
    assert resolved == ResolvedProfile(
        provider=LLMProvider.ANTHROPIC,
        model=PROFILE_MODEL,
        temperature=0.4,
        max_output_tokens=None,
    )
    # The fallback pair is configured and is NOT what was used.
    assert resolved.model != settings.llm_model


def test_a_profile_carries_its_own_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings(
        monkeypatch,
        PROVIDER="anthropic",
        MODEL=FALLBACK_MODEL,
        PROFILES=_profiles_json(
            PROFILE_UNIT_A_SCORE, model=PROFILE_MODEL, max_output_tokens=128
        ),
    )
    assert resolve_profile(settings, PROFILE_UNIT_A_SCORE).max_output_tokens == 128


# --- the fallback rule ------------------------------------------------------


def test_an_unconfigured_name_falls_back_to_the_single_model_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that runs one model configures the pair and no profile."""
    settings = _settings(monkeypatch, PROVIDER="anthropic", MODEL=FALLBACK_MODEL)
    assert resolve_profile(settings, PROFILE_UNIT_A_CLASSIFY) == ResolvedProfile(
        provider=LLMProvider.ANTHROPIC,
        model=FALLBACK_MODEL,
        temperature=0.0,
        max_output_tokens=None,
    )


def test_the_fallback_serves_names_other_profiles_are_configured_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A table that configures ONE task leaves the other two on the pair, which
    # is the shape "route the expensive pass elsewhere" actually takes.
    settings = _settings(
        monkeypatch,
        PROVIDER="anthropic",
        MODEL=FALLBACK_MODEL,
        PROFILES=_profiles_json(PROFILE_UNIT_A_VAGUE, model=PROFILE_MODEL),
    )
    assert resolve_profile(settings, PROFILE_UNIT_A_VAGUE).model == PROFILE_MODEL
    assert resolve_profile(settings, PROFILE_UNIT_A_SCORE).model == FALLBACK_MODEL
    assert resolve_profile(settings, PROFILE_UNIT_A_CLASSIFY).model == FALLBACK_MODEL


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"PROVIDER": "anthropic"},  # no model
        {"MODEL": FALLBACK_MODEL},  # no provider
    ],
    ids=["neither", "provider-only", "model-only"],
)
def test_no_profile_and_no_fallback_is_the_configuration_error(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    """The same fixed message get_llm_client() reports, for the same reason."""
    settings = _settings(monkeypatch, **env)
    with pytest.raises(LLMConfigurationError) as exc:
        resolve_profile(settings, PROFILE_UNIT_A_CLASSIFY)
    assert str(exc.value) == "llm_not_configured"


def test_a_configured_profile_does_not_need_the_fallback_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Profiles alone are a complete configuration: the pair is the fallback, not
    # a prerequisite.
    settings = _settings(
        monkeypatch,
        PROFILES=_profiles_json(PROFILE_UNIT_A_SCORE, model=PROFILE_MODEL),
    )
    assert settings.llm_provider is None
    assert resolve_profile(settings, PROFILE_UNIT_A_SCORE).model == PROFILE_MODEL


# --- validation at settings construction, not at first call -----------------


def test_malformed_profiles_json_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError):
        _settings(monkeypatch, PROFILES='{"unit_a.vague": {')


@pytest.mark.parametrize("temperature", [-0.1, 1.1, 2.0])
def test_temperature_outside_zero_to_one_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, temperature: float
) -> None:
    with pytest.raises(ConfigError):
        _settings(
            monkeypatch,
            PROFILES=_profiles_json(
                PROFILE_UNIT_A_VAGUE, model=PROFILE_MODEL, temperature=temperature
            ),
        )


@pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0])
def test_the_ends_of_the_temperature_range_are_accepted(
    monkeypatch: pytest.MonkeyPatch, temperature: float
) -> None:
    settings = _settings(
        monkeypatch,
        PROFILES=_profiles_json(
            PROFILE_UNIT_A_VAGUE, model=PROFILE_MODEL, temperature=temperature
        ),
    )
    assert resolve_profile(settings, PROFILE_UNIT_A_VAGUE).temperature == temperature


def test_a_profile_with_an_empty_model_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The same rule llm_model already has: no drifting default, and an empty
    # value is refused rather than falling through to the pair.
    with pytest.raises(ConfigError):
        _settings(monkeypatch, PROFILES=_profiles_json(PROFILE_UNIT_A_VAGUE, model=""))


def test_an_unknown_provider_in_a_profile_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ConfigError):
        _settings(
            monkeypatch,
            # Not "openai": that became a real provider with the adapter
            # (item 76). The value has to be a name no LLMProvider member holds.
            PROFILES='{"unit_a.vague": {"provider": "mistral", "model": "m"}}',
        )


def test_no_profiles_configured_is_an_empty_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _settings(monkeypatch).llm_profiles == {}


# --- the ceiling rule: a profile may lower, never raise ---------------------


def _resolved(max_output_tokens: int | None) -> ResolvedProfile:
    return ResolvedProfile(
        provider=LLMProvider.ANTHROPIC,
        model=PROFILE_MODEL,
        temperature=0.0,
        max_output_tokens=max_output_tokens,
    )


def test_a_profile_ceiling_above_the_task_ceiling_does_not_raise_it() -> None:
    """The task constant is sized against the longest answer the task can
    produce; a profile cannot buy it room it has no use for."""
    assert _resolved(4096).effective_max_output_tokens(256) == 256


def test_a_profile_ceiling_below_the_task_ceiling_lowers_it() -> None:
    assert _resolved(128).effective_max_output_tokens(256) == 128


def test_no_profile_ceiling_leaves_the_task_ceiling_alone() -> None:
    assert _resolved(None).effective_max_output_tokens(1024) == 1024


def test_an_equal_ceiling_is_the_same_number() -> None:
    assert _resolved(64).effective_max_output_tokens(64) == 64


# --- the call sites name known profiles and nothing else --------------------

_UNIT_MODULES = sorted(pathlib.Path("src/dodeal_ai/units").rglob("*.py"))
# `profile=<name>` or `profile=<literal>`. The forwarding parameter in
# llm_call.py spells it `profile=profile` and is the one allowed name that is
# not a constant -- everything else must resolve to a KNOWN_PROFILES value.
_PROFILE_ARG = re.compile(
    r"(?<![A-Za-z0-9_])profile=(\"[^\"]*\"|'[^']*'|[A-Za-z_][A-Za-z0-9_]*)"
)
_FORWARDING = "profile"


def _profile_arguments() -> list[tuple[str, int, str]]:
    return [
        (module.as_posix(), number, match.group(1))
        for module in _UNIT_MODULES
        for number, line in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        )
        for match in [_PROFILE_ARG.search(line)]
        if match is not None
    ]


def test_the_unit_names_a_profile_at_all() -> None:
    # Fail closed on a rename: a grep that matched nothing would make the test
    # below pass by searching an empty set.
    constants = [a for a in _profile_arguments() if a[2] != _FORWARDING]
    assert len(constants) >= len(KNOWN_PROFILES), constants


def test_every_profile_named_under_units_is_a_known_profile() -> None:
    """No call site may invent a name: the table is the vocabulary."""
    offenders = []
    for path, number, argument in _profile_arguments():
        if argument == _FORWARDING:
            continue  # llm_call.py's pass-through to the seam
        if argument.startswith(('"', "'")):
            value: object = argument[1:-1]
        else:
            value = getattr(profiles_module, argument, None)
        if value not in KNOWN_PROFILES:
            offenders.append(f"{path}:{number}: profile={argument}")
    assert not offenders, "a profile name that is not in KNOWN_PROFILES:\n" + "\n".join(
        offenders
    )


def test_the_five_names_are_distinct_and_namespaced() -> None:
    assert len(set(KNOWN_PROFILES)) == 5
    assert all(name.startswith(("unit_a.", "unit_b.")) for name in KNOWN_PROFILES)
