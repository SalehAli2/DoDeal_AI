"""Register item 94, read statically: the image runs as a non-root user, checks
/health, and serves through the proxy-aware entrypoint. No image is built."""

from __future__ import annotations

import pathlib

import pytest

from dodeal_ai import serve
from dodeal_ai.core.config import get_settings

_DOCKERFILE = pathlib.Path("Dockerfile")
_DOCKERIGNORE = pathlib.Path(".dockerignore")


def _final_stage() -> list[str]:
    """The instructions after the last FROM, continuation lines joined."""
    joined = _DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    lines = [line.strip() for line in joined.splitlines()]
    instructions = [line for line in lines if line and not line.startswith("#")]
    last_from = max(i for i, line in enumerate(instructions) if line.startswith("FROM"))
    return instructions[last_from:]


def _instruction(name: str) -> list[str]:
    return [line for line in _final_stage() if line.split()[0] == name]


def test_the_final_stage_runs_as_a_non_root_user():
    """One USER, numeric, not root, and set before the command runs."""
    (user,) = _instruction("USER")
    uid = user.split()[1].split(":")[0]
    assert uid.isdigit() and int(uid) != 0
    stage = _final_stage()
    assert stage.index(user) < stage.index(_instruction("CMD")[0])


def test_the_final_stage_installs_ffmpeg_before_dropping_root():
    """The call workers refuse to start without ffmpeg (audio.py)."""
    stage = _final_stage()
    (install,) = [line for line in _instruction("RUN") if "ffmpeg" in line]
    assert "--no-install-recommends" in install
    assert stage.index(install) < stage.index(_instruction("USER")[0])


def test_the_image_checks_health_on_the_health_route():
    """A HEALTHCHECK exists and asks /health, not /ready."""
    (check,) = _instruction("HEALTHCHECK")
    assert "/health" in check and "/ready" not in check
    assert "--interval=" in check and "--timeout=" in check


def test_the_command_is_the_proxy_aware_entrypoint():
    """The container starts dodeal_ai.serve, never bare uvicorn without its flags."""
    (command,) = _instruction("CMD")
    assert "dodeal_ai.serve" in command


@pytest.mark.parametrize("pattern", [".env", "tests"])
def test_the_dockerignore_keeps_secrets_and_tests_out(pattern):
    """.env and tests/ never reach the build context."""
    lines = {
        line.strip().rstrip("/")
        for line in _DOCKERIGNORE.read_text(encoding="utf-8").splitlines()
    }
    assert pattern in lines


def test_serve_passes_proxy_headers_the_setting_and_a_30s_shutdown(monkeypatch):
    """uvicorn gets --proxy-headers, the allow-list from Settings and 30 s to drain."""
    monkeypatch.setenv("DODEAL_FORWARDED_ALLOW_IPS", "10.0.0.5,10.0.0.6")
    get_settings.cache_clear()
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        serve.uvicorn, "run", lambda app, **kwargs: calls.append((app, kwargs))
    )

    serve.main()

    ((app, kwargs),) = calls
    assert app == "dodeal_ai.main:app"
    assert kwargs["proxy_headers"] is True
    assert kwargs["forwarded_allow_ips"] == "10.0.0.5,10.0.0.6"
    assert kwargs["timeout_graceful_shutdown"] == 30
    get_settings.cache_clear()


def test_the_forwarded_allow_list_defaults_to_loopback_only():
    """Unset, only a proxy on the same host is trusted with forwarded headers."""
    assert get_settings().forwarded_allow_ips == "127.0.0.1"


def test_the_drain_follows_the_judgement_deadline(monkeypatch):
    """Register item 94: deadline plus five, rounded up -- 12.3 s drains in 18."""
    monkeypatch.setenv("DODEAL_JUDGEMENT_DEADLINE_SECONDS", "12.3")
    get_settings.cache_clear()
    calls: list[dict] = []
    monkeypatch.setattr(
        serve.uvicorn, "run", lambda app, **kwargs: calls.append(kwargs)
    )

    serve.main()

    assert calls[0]["timeout_graceful_shutdown"] == 18
    get_settings.cache_clear()
