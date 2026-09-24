"""What a production process refuses to start with (core safety): checked by
the API's lifespan and every call worker's startup, before anything opens.

Under DODEAL_ENVIRONMENT=production the process refuses to start, naming every
reason at once, with:

  demo_local_audio_on   CALL_DEMO_ALLOW_LOCAL_AUDIO on: a push could aim the
                        service at its own network, over http
  backend_scheme_http   BACKEND_SCHEME http: every DD-API-KEY in clear
  stt_provider_fake     CALL_STT_PROVIDER fake: calls would get invented words
  service_key_missing   no SERVICE_JWT_SIGNING_KEY: every service call is 401

and an HS256 service key logs one WARNING: a shared secret that can mint
tokens, where RS256 and ES256 hold only a public key. development and staging
refuse nothing: the demo and a laptop need every one of these.

The refusal carries fixed codes only, never a value.
"""

from __future__ import annotations

import logging

from dodeal_ai.core.config import ConfigError, Settings

PRODUCTION = "production"

DEMO_LOCAL_AUDIO_ON = "demo_local_audio_on"
BACKEND_SCHEME_HTTP = "backend_scheme_http"
STT_PROVIDER_FAKE = "stt_provider_fake"
SERVICE_KEY_MISSING = "service_key_missing"

_logger = logging.getLogger("dodeal_ai.startup")


class UnsafeForProduction(ConfigError):
    """A production process configured as only a demo may be."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = tuple(reasons)
        super().__init__(f"unsafe_for_production:{','.join(reasons)}")


def refusals(settings: Settings) -> list[str]:
    """Every reason production would refuse `settings`, in a fixed order."""
    return [
        reason
        for reason, unsafe in (
            (DEMO_LOCAL_AUDIO_ON, settings.call_demo_allow_local_audio),
            (BACKEND_SCHEME_HTTP, settings.backend_scheme == "http"),
            (STT_PROVIDER_FAKE, settings.call_stt_provider == "fake"),
            (SERVICE_KEY_MISSING, settings.service_jwt_signing_key is None),
        )
        if unsafe
    ]


def check_production_safety(settings: Settings) -> None:
    """UnsafeForProduction for a production process with any refusal; one
    WARNING for an HS256 service key. Nothing outside production."""
    if settings.environment != PRODUCTION:
        return
    found = refusals(settings)
    if found:
        raise UnsafeForProduction(found)
    if settings.service_jwt_algorithm == "HS256":
        _logger.warning(
            "service_key_hs256 -- the service token is verified with a shared "
            "secret that can also mint one; RS256 or ES256 holds a public key.",
            extra={"event": "service_key_hs256"},
        )
