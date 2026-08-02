"""Shared fixtures for security-gate tests.

Aligns the runtime signing key/iss/aud with the test-token helper's constants,
so tokens minted by tests/helpers/tokens.py verify against a real Settings
object. Test-only wiring — no real secret involved.
"""
from __future__ import annotations

import pytest

from dodeal_ai.core.config import Settings
from tests.helpers import tokens


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
        jwt_issuer=tokens.TEST_ISS,
        jwt_audience=tokens.TEST_AUD,
    )


@pytest.fixture
def verifier(settings):
    from dodeal_ai.core.auth.verify import JwtVerifier

    return JwtVerifier(settings)