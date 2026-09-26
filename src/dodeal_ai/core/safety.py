"""What a production or staging process refuses to start with (core safety):
checked by the API's lifespan and every call worker's startup, before anything
opens.

Under DODEAL_ENVIRONMENT=production or staging the process refuses to start,
naming every reason at once, with:

  demo_local_audio_on   CALL_DEMO_ALLOW_LOCAL_AUDIO on: a push could aim the
                        service at its own network, over http
  backend_scheme_http   BACKEND_SCHEME http: every DD-API-KEY in clear
  stt_provider_fake     CALL_STT_PROVIDER fake: calls would get invented words
  service_key_missing   no SERVICE_JWT_SIGNING_KEY: every service call is 401
  llm_base_url_http     an LLM base URL (the pair, the fallback or a registry
                        provider) not https: the key and every prompt in clear
  stt_base_url_http     an STT base URL (the default or a named profile) not
                        https: the key and every recording in clear

and an HS256 service key logs one WARNING in production: a shared secret that
can mint tokens, where RS256 and ES256 hold only a public key. development
refuses nothing: the demo and a laptop need every one of these.

The refusal carries fixed codes only, never a value.
"""

from __future__ import annotations

import logging

from dodeal_ai.core.config import ConfigError, Settings

PRODUCTION = "production"
STAGING = "staging"
# Where the refusals apply: staging runs real tenants' calls on real keys too.
REFUSING_ENVIRONMENTS = frozenset({PRODUCTION, STAGING})

DEMO_LOCAL_AUDIO_ON = "demo_local_audio_on"
BACKEND_SCHEME_HTTP = "backend_scheme_http"
STT_PROVIDER_FAKE = "stt_provider_fake"
SERVICE_KEY_MISSING = "service_key_missing"
LLM_BASE_URL_HTTP = "llm_base_url_http"
STT_BASE_URL_HTTP = "stt_base_url_http"

_logger = logging.getLogger("dodeal_ai.startup")


class UnsafeForProduction(ConfigError):
    """A production or staging process configured as only a demo may be."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(f"unsafe_for_production:{','.join(reasons)}")


def _not_https(url: str | None) -> bool:
    """A set base URL whose scheme is not https; None uses a built-in https one."""
    return url is not None and not url.lower().startswith("https://")


def _llm_urls(settings: Settings) -> list[str | None]:
    """Every LLM base URL: the DODEAL_LLM_* pair's, the fallback's, the registry's."""
    return [
        settings.llm_base_url,
        settings.llm_fallback_base_url,
        *(spec.base_url for spec in settings.llm_providers.values()),
    ]


def _stt_urls(settings: Settings) -> list[str | None]:
    """Every STT base URL: the default profile's and each named profile's."""
    return [
        settings.call_stt_base_url,
        *(profile.base_url for profile in settings.call_stt_profiles.values()),
    ]


def refusals(settings: Settings) -> list[str]:
    """Every reason production or staging would refuse `settings`, in a fixed
    order."""
    return [
        reason
        for reason, unsafe in (
            (DEMO_LOCAL_AUDIO_ON, settings.call_demo_allow_local_audio),
            (BACKEND_SCHEME_HTTP, settings.backend_scheme == "http"),
            (STT_PROVIDER_FAKE, settings.call_stt_provider == "fake"),
            (SERVICE_KEY_MISSING, settings.service_jwt_signing_key is None),
            (LLM_BASE_URL_HTTP, any(map(_not_https, _llm_urls(settings)))),
            (STT_BASE_URL_HTTP, any(map(_not_https, _stt_urls(settings)))),
        )
        if unsafe
    ]


def check_production_safety(settings: Settings) -> None:
    """UnsafeForProduction for a production or staging process with any
    refusal; one WARNING for an HS256 service key in production. Nothing in
    development."""
    if settings.environment not in REFUSING_ENVIRONMENTS:
        return
    found = refusals(settings)
    if found:
        raise UnsafeForProduction(found)
    if settings.environment == PRODUCTION and settings.service_jwt_algorithm == "HS256":
        _logger.warning(
            "service_key_hs256 -- the service token is verified with a shared "
            "secret that can also mint one; RS256 or ES256 holds a public key.",
            extra={"event": "service_key_hs256"},
        )
