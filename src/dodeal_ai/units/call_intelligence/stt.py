"""The speech-to-text profiles a call worker runs (Unit B): "default", from
DODEAL_CALL_STT_*, and each of DODEAL_CALL_STT_PROFILES, built once at start.

A tenant's unit_b stt_profile names one (config.py refuses any other). Each
key is read here and only here: the default's from DODEAL_CALL_STT_API_KEY, a
named profile's from the variable it names. A profile that cannot be built
refuses the worker's start, whichever tenant uses it. The fake is never built:
the demo hands it in, and it is refused unless CALL_DEMO_ALLOW_LOCAL_AUDIO is on.
"""

from __future__ import annotations

import os

import httpx

from dodeal_ai.core.config import ConfigError, Settings, SttProfile
from dodeal_ai.units.call_intelligence.gemini import GeminiTranscriber
from dodeal_ai.units.call_intelligence.transcriber import (
    DEFAULT_STT_PROFILE,
    HandedTranscriberRefused,
    Transcriber,
    TranscriberNotConfigured,
    stt_profile_names,
)


def _api_key(name: str, profile: SttProfile, settings: Settings) -> str:
    """The profile's key: the one read of DODEAL_CALL_STT_API_KEY or of the
    variable a named profile names. ConfigError when empty."""
    if name == DEFAULT_STT_PROFILE:
        secret = settings.call_stt_api_key
        key = "" if secret is None else secret.get_secret_value()
    else:
        key = os.environ.get(profile.api_key_env, "")
    if not key:
        raise ConfigError(f"stt_api_key_missing:{name}")
    return key


def build_profile(
    name: str, profile: SttProfile, settings: Settings, http: httpx.AsyncClient
) -> Transcriber:
    """One STT profile's transcriber; ConfigError when it cannot be built."""
    if profile.provider != "gemini":
        raise TranscriberNotConfigured()
    return GeminiTranscriber(
        model=profile.model,
        api_key=_api_key(name, profile, settings),
        http=http,
        base_url=profile.base_url,
        # The job's own deadline bounds a transcription; this only never cuts
        # one shorter.
        timeout_seconds=settings.call_job_timeout_seconds,
    )


def build_transcribers(
    settings: Settings, http: httpx.AsyncClient
) -> dict[str, Transcriber]:
    """Every STT profile's transcriber, by name; ConfigError for any that
    cannot be built -- the fake default among them."""
    if settings.call_stt_provider == "fake":
        raise TranscriberNotConfigured()
    default = SttProfile(
        provider=settings.call_stt_provider,
        base_url=settings.call_stt_base_url,
        model=settings.call_stt_model,
        api_key_env="DODEAL_CALL_STT_API_KEY",
    )
    profiles = {DEFAULT_STT_PROFILE: default, **settings.call_stt_profiles}
    return {
        name: build_profile(name, profile, settings, http)
        for name, profile in profiles.items()
    }


def select_transcribers(
    settings: Settings, handed: Transcriber | None, http: httpx.AsyncClient
) -> dict[str, Transcriber]:
    """A worker's transcribers: the factory's, or one handed in -- the fake,
    for the demo -- for every profile, only while CALL_DEMO_ALLOW_LOCAL_AUDIO
    is on. Off, the worker refuses to start rather than invent words."""
    if handed is None:
        return build_transcribers(settings, http)
    if not settings.call_demo_allow_local_audio:
        raise HandedTranscriberRefused()
    return dict.fromkeys(stt_profile_names(settings), handed)
