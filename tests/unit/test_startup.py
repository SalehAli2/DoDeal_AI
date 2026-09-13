"""Startup signal for a misprovisioned deployment (audit finding F2): an empty
dd_api_keys map means nothing can reach the backend, and that must be loud at
startup, not discovered later as every data call failing.

WHY THESE READ THE JSON STREAM AND NOT `caplog`. The line is emitted AFTER
`configure_logging()` (audit §6.9) so that it goes out as a formatted JSON
object like every other startup event, rather than through whatever handler
`logging` happened to have. But `configure_logging()` does `root.handlers =
[handler]` -- it REPLACES the root's handlers -- and `caplog` works by
installing its own handler on the root. So caplog's handler is evicted during
startup and sees nothing, even though the record was emitted correctly.

Attaching to the `dodeal_ai` logger instead survives, because
`configure_logging()` sets that logger's LEVEL and never touches its handlers.
It is also the more honest test: it asserts the shape a log collector actually
receives, not the shape an in-process capture fixture reconstructs.

Same pattern as `json_capture` in tests/unit/test_judgement_pipeline.py and
`log_capture` in tests/security/test_unit_a_injection.py.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.llm.profiles import PROFILE_UNIT_A_CLASSIFY
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import LLM_CALLS_PER_JUDGEMENT, _llm_limits, app

STARTUP_LOGGER = "dodeal_ai.startup"
EVENT = "backend_keys_missing"


@pytest.fixture
def json_lines():
    """The real JsonFormatter over the dodeal_ai tree, as parsed objects.

    Attached to `dodeal_ai` rather than to the root, so it survives the
    `configure_logging()` that runs inside the app's lifespan.
    """
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield lambda: [json.loads(x) for x in stream.getvalue().splitlines()]
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _events(lines: list[dict], name: str) -> list[dict]:
    """Every captured line whose message names `name`. The formatter has no
    dedicated event field -- the event IS the message, and this unit's messages
    are a fixed vocabulary with the code first."""
    return [line for line in lines if name in str(line.get("message", ""))]


def test_backend_keys_missing_logs_error_when_map_is_empty(monkeypatch, json_lines):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.delenv("DODEAL_DD_API_KEYS", raising=False)
    get_settings.cache_clear()

    with TestClient(app):
        pass

    found = _events(json_lines(), EVENT)
    assert found, "no backend_keys_missing line reached the JSON stream"
    # A real JSON object on the collector's stream, at ERROR, from the startup
    # logger -- which is the whole point of emitting it after configure_logging.
    assert found[0]["level"] == "ERROR"
    assert found[0]["logger"] == STARTUP_LOGGER
    get_settings.cache_clear()


def test_backend_keys_missing_not_logged_when_map_is_non_empty(monkeypatch, json_lines):
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_DD_API_KEYS", '{"tenant-a":"a-real-key"}')
    get_settings.cache_clear()

    with TestClient(app):
        pass

    # PROVE THE CHANNEL WORKS FIRST. An absence assertion over a capture that
    # sees nothing passes for the wrong reason -- which is exactly how this
    # test survived the §6.9 move while its partner failed. The probe is logged
    # after startup, so it goes through the same post-configure_logging channel
    # the real line would have used.
    logging.getLogger(STARTUP_LOGGER).error("startup_probe_line count=0")

    lines = json_lines()
    assert _events(lines, "startup_probe_line"), (
        "the capture channel saw nothing at all, so the absence assertion below "
        "would pass vacuously"
    )
    assert _events(lines, EVENT) == []
    get_settings.cache_clear()


# --- the LLM client lifespan builds (item 84) -------------------------------

LLM_EVENT = "llm_not_configured"
_PROFILE = PROFILE_UNIT_A_CLASSIFY


def _llm_env(monkeypatch, **extra: str) -> None:
    """A configured provider, plus whatever the case is actually about."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    for key in ("PROVIDER", "MODEL", "API_KEY", "PROFILES", "BASE_URL"):
        monkeypatch.delenv(f"DODEAL_LLM_{key}", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(f"DODEAL_LLM_{key}", value)
    get_settings.cache_clear()


def test_unset_provider_starts_the_app_and_says_so_loudly(monkeypatch, json_lines):
    """PERMISSIVE startup, the dd_api_keys precedent: no provider is
    provisioned yet (Step 0), and a service that refuses to start cannot serve
    /health, /ready or the gate chain. Loud, so it is never discovered later as
    every judgement 503ing."""
    _llm_env(monkeypatch)

    with TestClient(app):
        assert app.state.llm is None

    found = _events(json_lines(), LLM_EVENT)
    assert found, "no llm_not_configured line reached the JSON stream"
    assert found[0]["level"] == "ERROR"
    assert found[0]["logger"] == STARTUP_LOGGER
    get_settings.cache_clear()


def test_a_configured_provider_builds_one_client(monkeypatch):
    _llm_env(monkeypatch, PROVIDER="groq", MODEL="pinned-model", API_KEY="k")

    with TestClient(app):
        first = app.state.llm
        assert first is not None
        # ONE client for the process, not one per request: the same object is
        # still there after traffic has gone through the app.
        assert app.state.llm is first
    get_settings.cache_clear()


def test_the_pooled_client_is_closed_on_shutdown(monkeypatch):
    """A client left open leaks sockets to a third party. Closing is asserted
    on the real object rather than a spy, so a future edit that closes the
    wrong thing cannot pass."""
    _llm_env(monkeypatch, PROVIDER="groq", MODEL="pinned-model", API_KEY="k")

    with TestClient(app):
        http = app.state.http
        assert not http.is_closed

    assert http.is_closed
    get_settings.cache_clear()


def test_the_pool_is_sized_from_max_inflight(monkeypatch):
    """Sized against PASSES, not requests -- see LLM_CALLS_PER_JUDGEMENT."""
    _llm_env(monkeypatch, PROVIDER="groq", MODEL="pinned-model", API_KEY="k")
    monkeypatch.setenv("DODEAL_MAX_INFLIGHT", "7")
    get_settings.cache_clear()

    limits = _llm_limits(get_settings())

    assert limits.max_connections == 7 * LLM_CALLS_PER_JUDGEMENT
    assert limits.max_keepalive_connections == limits.max_connections
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("env", "fragment"),
    [
        ({"PROVIDER": "groq", "MODEL": "m"}, "llm_api_key_missing"),
        (
            {"PROVIDER": "gemini", "MODEL": "m", "API_KEY": "k"},
            "llm_provider_not_supported",
        ),
        ({"PROVIDER": "groq", "API_KEY": "k"}, "llm_not_configured"),
    ],
    ids=["no-key", "no-adapter", "no-model"],
)
def test_a_configured_but_broken_provider_refuses_to_start(monkeypatch, env, fragment):
    """Somebody MEANT to configure this and got it wrong. That is not a state to
    serve traffic in, so it is a genuine ConfigError and the app does not come
    up -- the other half of item 84 from the permissive case above."""
    _llm_env(monkeypatch, **env)

    with pytest.raises(ConfigError) as caught, TestClient(app):
        pass

    assert fragment in str(caught.value)
    get_settings.cache_clear()


def test_a_bad_profile_refuses_to_start(monkeypatch):
    """THE STARTUP SWEEP. A cross-vendor profile would otherwise post an
    Anthropic model id to Groq on the first judgement that named it -- days
    later, on one task only. Resolved once here instead."""
    _llm_env(
        monkeypatch,
        PROVIDER="groq",
        MODEL="pinned-model",
        API_KEY="k",
        PROFILES=json.dumps(
            {_PROFILE: {"provider": "anthropic", "model": "claude-something"}}
        ),
    )

    with pytest.raises(ConfigError) as caught, TestClient(app):
        pass

    assert "llm_profile_provider_mismatch" in str(caught.value)
    # The provider is named; no value from the profile is interpolated.
    assert "claude-something" not in str(caught.value)
    get_settings.cache_clear()


def test_a_good_profile_starts(monkeypatch):
    """The sweep must not refuse a profile a real call would accept."""
    _llm_env(
        monkeypatch,
        PROVIDER="groq",
        MODEL="pinned-model",
        API_KEY="k",
        PROFILES=json.dumps(
            {_PROFILE: {"provider": "groq", "model": "m", "temperature": 0.5}}
        ),
    )

    with TestClient(app):
        assert app.state.llm is not None
    get_settings.cache_clear()


# --- the insecure backend scheme (register item 78a) ------------------------

SCHEME_EVENT = "backend_scheme_insecure"


def test_http_backend_scheme_logs_an_error(monkeypatch, json_lines):
    """http is a KEY DISCLOSURE risk, not a misconfiguration: the per-tenant
    DD-API-KEY rides every backend request and http puts it in clear."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_BACKEND_SCHEME", "http")
    get_settings.cache_clear()

    with TestClient(app):
        pass

    found = _events(json_lines(), SCHEME_EVENT)
    assert found, "no backend_scheme_insecure line reached the JSON stream"
    assert found[0]["level"] == "ERROR"
    assert found[0]["logger"] == STARTUP_LOGGER
    get_settings.cache_clear()


def test_http_backend_scheme_line_names_no_key_tenant_or_url(monkeypatch, json_lines):
    """The line travels to a collector. It must carry the FACT and nothing that
    identifies whose key, which tenant or which host is exposed."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.setenv("DODEAL_BACKEND_SCHEME", "http")
    monkeypatch.setenv("DODEAL_BACKEND_BASE_DOMAIN", "leaky.example.test")
    monkeypatch.setenv("DODEAL_DD_API_KEYS", '{"tenant-a":"a-real-looking-key"}')
    get_settings.cache_clear()

    with TestClient(app):
        pass

    message = str(_events(json_lines(), SCHEME_EVENT)[0]["message"])
    assert "a-real-looking-key" not in message
    assert "tenant-a" not in message
    assert "leaky.example.test" not in message
    assert "http://" not in message
    get_settings.cache_clear()


def test_the_default_scheme_logs_nothing(monkeypatch, json_lines):
    """https is the default and the overwhelmingly common case. A line on every
    healthy startup is a line nobody reads on the one unhealthy startup."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    monkeypatch.delenv("DODEAL_BACKEND_SCHEME", raising=False)
    get_settings.cache_clear()

    with TestClient(app):
        pass

    # PROVE THE CHANNEL FIRST, the backend_keys_missing precedent: an absence
    # assertion over a capture that sees nothing passes for the wrong reason.
    logging.getLogger(STARTUP_LOGGER).error("startup_probe_line count=0")

    lines = json_lines()
    assert _events(lines, "startup_probe_line"), (
        "the capture channel saw nothing at all, so the absence assertion below "
        "would pass vacuously"
    )
    assert _events(lines, SCHEME_EVENT) == []
    get_settings.cache_clear()
