"""Shared fixtures for security-gate tests.

Aligns the runtime signing key/iss/aud with the test-token helper's constants,
so tokens minted by tests/helpers/tokens.py verify against a real Settings
object. Test-only wiring — no real secret involved.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from dodeal_ai.core.config import Settings
from dodeal_ai.core.logging_config import JsonFormatter
from tests.helpers import tokens


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )


@pytest.fixture
def verifier(settings):
    from dodeal_ai.core.auth.verify import JwtVerifier

    return JwtVerifier(settings)


@pytest.fixture
def json_log():
    """Capture the FORMATTED JSON lines for the whole `dodeal_ai` logger tree.

    Audit fields travel as `extra=` and are merged into the line by
    core/logging_config.JsonFormatter, so `record.getMessage()` is now just
    "auth_decision" -- the fields only exist once formatted. Tests therefore
    assert on the same JSON a log collector would parse. Yields a callable
    returning the lines captured so far, each already parsed; filter by
    `line["logger"]` for one logger.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(line) for line in stream.getvalue().splitlines()]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)
