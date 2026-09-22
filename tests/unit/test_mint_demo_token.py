"""The demo minter (register item D1): service tokens for the service routes,
user tokens for the rest, both signed with the keys the service verifies with."""

from __future__ import annotations

import json
import sys

import pytest

from dodeal_ai.core.auth.service import ServiceTokenVerifier
from dodeal_ai.core.auth.verify import JwtVerifier, verify_token
from dodeal_ai.core.config import ConfigError, Settings
from scripts import mint_demo_token as minter

_USER_KEY = "demo-test-user-key-at-least-32-bytes-long"
_SERVICE_KEY = "demo-test-service-key-at-least-32-bytes"


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "jwt_signing_key": _USER_KEY,
        "service_jwt_algorithm": "HS256",
        "service_jwt_signing_key": _SERVICE_KEY,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_a_minted_service_token_verifies_with_the_same_settings() -> None:
    """The guard: what the minter signs, ServiceTokenVerifier accepts."""
    settings = _settings()
    token = minter.mint_service_token(settings, "tenant-a")
    assert ServiceTokenVerifier(settings).verify(token).tenant == "tenant-a"


def test_the_service_claims_are_the_five_required_and_five_minutes() -> None:
    """iss, aud, subdomain, iat and exp, living 300 seconds."""
    claims = minter.service_payload(_settings(), "tenant-a")
    assert set(claims) == {"iss", "aud", "subdomain", "iat", "exp"}
    assert claims["exp"] - claims["iat"] == 300


def test_a_shorter_deployment_maximum_shortens_the_token() -> None:
    """A token longer than the service accepts would be refused on arrival."""
    claims = minter.service_payload(
        _settings(service_jwt_max_lifetime_seconds=60), "tenant-a"
    )
    assert claims["exp"] - claims["iat"] == 60


@pytest.mark.parametrize(
    "overrides",
    [{"service_jwt_signing_key": None}, {"service_jwt_algorithm": "RS256"}],
)
def test_no_shared_secret_means_no_service_token(overrides: dict) -> None:
    """Under RS256 or with no key the demo cannot mint, and says so."""
    with pytest.raises(ConfigError):
        minter.mint_service_token(_settings(**overrides), "tenant-a")


def _run(monkeypatch, capsys, route: str) -> tuple[int, str]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", _USER_KEY)
    monkeypatch.setenv("DODEAL_SERVICE_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("DODEAL_SERVICE_JWT_SIGNING_KEY", _SERVICE_KEY)
    monkeypatch.setattr(
        sys, "argv", ["mint", "--route", route, "--env-file", "no-such-file.env"]
    )
    monkeypatch.setattr(minter, "DEFAULT_ENV_STACK", ("no-such.env", ".env.demo"))
    code = minter.main()
    return code, capsys.readouterr().out


def _token_from(out: str) -> str:
    lines = out.splitlines()
    return lines[lines.index("TOKEN") + 1]


@pytest.mark.parametrize("route", sorted(minter.SERVICE_ROUTES))
def test_each_service_route_gets_a_service_token(monkeypatch, capsys, route) -> None:
    """direct, history, brief, measures and config: the CRM's token."""
    code, out = _run(monkeypatch, capsys, route)
    assert code == 0
    assert "Token:      service (the CRM's)" in out
    token = _token_from(out)
    assert ServiceTokenVerifier(_settings()).verify(token).tenant == "tenant-a"


@pytest.mark.parametrize("route", ["fetch", "probe"])
def test_fetch_and_the_versions_probe_keep_user_tokens(
    monkeypatch, capsys, route
) -> None:
    """The user chain's routes still get a person's token."""
    code, out = _run(monkeypatch, capsys, route)
    assert code == 0
    assert "Token:      user" in out
    identity = verify_token(_token_from(out), JwtVerifier(_settings()))
    assert identity.subject == "42"


def test_the_history_body_carries_the_note_date(monkeypatch, capsys) -> None:
    """The history curl line posts note_created_at with an offset."""
    _code, out = _run(monkeypatch, capsys, "history")
    curl = next(line for line in out.splitlines() if line.startswith("curl"))
    body = json.loads(curl.split("-d '", 1)[1].rstrip("'"))
    assert body["note_created_at"] == "2026-03-04T09:15:00+04:00"
    assert "-X POST" in curl


def test_the_get_routes_post_nothing(monkeypatch, capsys) -> None:
    """brief, measures and config are GETs with no body."""
    _code, out = _run(monkeypatch, capsys, "config")
    curl = next(line for line in out.splitlines() if line.startswith("curl"))
    assert "-X POST" not in curl and " -d " not in curl


def test_a_service_route_without_a_shared_secret_exits_two(monkeypatch, capsys) -> None:
    """The refusal is a message and exit 2, never a traceback."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", _USER_KEY)
    monkeypatch.setenv("DODEAL_SERVICE_JWT_ALGORITHM", "RS256")
    monkeypatch.delenv("DODEAL_SERVICE_JWT_SIGNING_KEY", raising=False)
    monkeypatch.setattr(
        sys, "argv", ["mint", "--route", "direct", "--env-file", "no-such-file.env"]
    )
    monkeypatch.setattr(minter, "DEFAULT_ENV_STACK", ("no-such.env", ".env.demo"))
    assert minter.main() == 2
    assert "only the" in capsys.readouterr().err
