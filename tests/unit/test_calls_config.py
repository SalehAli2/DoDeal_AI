"""The `unit_b` tenant section (register item 50): parsed and stored only,
every switch off by default, and an http callback or calls on with no audio
host refused -- in a file, at runtime, and by the admin route."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from dodeal_ai.core.config import ConfigError, get_settings
from dodeal_ai.core.tenant_config import (
    clear_tenant_configs,
    load_tenant_configs,
    override_key,
    register_section_parsers,
    tenant_section,
)
from dodeal_ai.main import TENANT_CONFIG_SECTIONS, app
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    CallsConfig,
    calls_section_of,
    parse_unit_b_section,
    resolve_calls_config,
)
from dodeal_ai.units.structured_intelligence.config import UNIT_A_SECTION
from tests.conftest import RedisFakes
from tests.helpers import tokens

CALLS_URL = "/api/v1/admin/tenant-config/unit_b"
UNIT_A_URL = "/api/v1/admin/tenant-config"
HISTORY_URL = "/api/v1/admin/tenant-config/history"
ON = {"calls_enabled": True, "audio_hosts": ["audio.tenant-a.example"]}


@pytest.fixture
def client(monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    yield TestClient(app)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _cleared() -> Iterator[None]:
    clear_tenant_configs()
    register_section_parsers(TENANT_CONFIG_SECTIONS)
    yield
    clear_tenant_configs()
    register_section_parsers(TENANT_CONFIG_SECTIONS)


def _headers(token: str | None = None) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token or tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


# --- the parser -------------------------------------------------------------


def test_the_default_has_every_switch_off_and_the_listed_values() -> None:
    """Nothing is on until a tenant asks, and the numbers are the agreed ones."""
    config = CallsConfig()
    assert not any(
        (
            config.calls_enabled,
            config.scoring_enabled,
            config.voice_id_enabled,
            config.number_detection_enabled,
            config.alarm_phrases_enabled,
            config.prosody_enabled,
        )
    )
    assert (
        config.callback_url,
        config.audio_hosts,
        config.min_transcribe_seconds,
        config.scoring_min_seconds,
        config.result_ttl_seconds,
        config.max_audio_bytes,
        config.priority_statuses,
        config.alarm_phrases,
        config.phone_country_code,
    ) == (
        None,
        frozenset(),
        30,
        120,
        259_200,
        209_715_200,
        frozenset({"qualified", "negotiation"}),
        frozenset(),
        "971",
    )


@pytest.mark.parametrize(
    "body",
    [
        {"callback_url": "http://crm.tenant-a.example/calls"},
        {"calls_enabled": True},
        {"calls_enabled": True, "audio_hosts": []},
    ],
    ids=["http-callback", "calls-on-no-host", "calls-on-empty-hosts"],
)
def test_the_guard_refuses_an_http_callback_or_calls_on_with_no_host(
    body: dict,
) -> None:
    """The two refusals the section exists for."""
    with pytest.raises(ValidationError):
        parse_unit_b_section(body)


@pytest.mark.parametrize(
    "body",
    [
        {"callback_url": "https://user:pw@crm.tenant-a.example/calls"},
        {"callback_url": "https:///calls"},
        {"callback_url": "https://[::1/calls"},
        {"audio_hosts": ["https://audio.example"]},
        {"audio_hosts": ["*.audio.example"]},
        {"audio_hosts": ["audio.example/path"]},
        {"audio_hosts": [7]},
        {"audio_hosts": "audio.example"},
        {"result_ttl_seconds": 259_201},
        {"max_audio_bytes": 209_715_201},
        {"min_transcribe_seconds": 200},
        {"priority_statuses": [""]},
        {"config_version": ""},
        {"phone_country_code": "0971"},
        {"phone_country_code": "+971"},
        {"phone_country_code": "9715"},
        {"phone_country_code": ""},
        {"unknown_switch": True},
    ],
)
def test_anything_else_malformed_is_refused(body: dict) -> None:
    """Credentials in a URL, a host that is not a bare name, a hold past 72 h, a
    recording past 200 MiB, a floor above the scoring floor, an unknown key."""
    with pytest.raises(ValidationError):
        parse_unit_b_section(body)


def test_a_full_section_parses_hosts_lowered_and_words_casefolded() -> None:
    """Hosts are compared exactly, so they are stored in one case."""
    config = parse_unit_b_section(
        {
            **ON,
            "audio_hosts": ["Audio.Tenant-A.Example"],
            "callback_url": "https://crm.tenant-a.example/calls",
            "priority_statuses": ["Qualified"],
            "alarm_phrases": ["Cancel The Deal"],
            "scoring_enabled": True,
        }
    )
    assert config.audio_hosts == frozenset({"audio.tenant-a.example"})
    assert config.priority_statuses == frozenset({"qualified"})
    assert config.alarm_phrases == frozenset({"cancel the deal"})
    assert config.scoring_enabled


def test_a_section_round_trips_through_its_view() -> None:
    """What GET shows parses back to the same config."""
    config = parse_unit_b_section({**ON, "alarm_phrases": ["b", "a"]})
    view = calls_section_of(config)
    assert view["alarm_phrases"] == ["a", "b"]
    assert parse_unit_b_section(view) == config


# --- the tenant file ---------------------------------------------------------


def test_a_tenant_file_section_is_installed(tmp_path: Path) -> None:
    (tmp_path / "tenant-a.json").write_text(
        json.dumps({UNIT_B_SECTION: ON}), encoding="utf-8"
    )
    load_tenant_configs(tmp_path, TENANT_CONFIG_SECTIONS)
    section = tenant_section("tenant-a", UNIT_B_SECTION)
    assert isinstance(section, CallsConfig) and section.calls_enabled


def test_an_invalid_file_section_refuses_startup_naming_tenant_and_section(
    tmp_path: Path,
) -> None:
    (tmp_path / "tenant-a.json").write_text(
        json.dumps({UNIT_B_SECTION: {"calls_enabled": True}}), encoding="utf-8"
    )
    with pytest.raises(ConfigError, match=r"^tenant_config_invalid:tenant-a:unit_b$"):
        load_tenant_configs(tmp_path, TENANT_CONFIG_SECTIONS)


async def test_a_tenant_with_nothing_set_resolves_the_default() -> None:
    assert await resolve_calls_config("tenant-a") == CallsConfig()


# --- the admin route ----------------------------------------------------------


def test_a_put_is_the_rules_in_force_and_get_shows_them(client: TestClient) -> None:
    put = client.put(CALLS_URL, json=ON, headers=_headers())
    assert put.status_code == 200
    got = client.get(CALLS_URL, headers=_headers()).json()
    assert (got["version"], got["policy_version"], got["source"]) == (
        put.json()["version"],
        put.json()["policy_version"],
        "override",
    )
    assert got[UNIT_B_SECTION]["calls_enabled"] is True
    assert got[UNIT_B_SECTION]["audio_hosts"] == ["audio.tenant-a.example"]


def test_get_with_nothing_set_is_the_default_all_off(client: TestClient) -> None:
    got = client.get(CALLS_URL, headers=_headers()).json()
    assert (got["source"], got["version"]) == ("default", None)
    assert got[UNIT_B_SECTION]["calls_enabled"] is False


@pytest.mark.parametrize(
    "body",
    [{"callback_url": "http://crm.example/calls"}, {"calls_enabled": True}, "[}"],
)
def test_a_refused_section_is_422_and_stores_nothing(
    client: TestClient, redis_fakes: RedisFakes, body: object
) -> None:
    content = body if isinstance(body, str) else json.dumps(body)
    response = client.put(CALLS_URL, content=content, headers=_headers())
    assert response.status_code == 422
    assert response.json()["reason"] == "invalid_tenant_config"
    assert override_key("tenant-a") not in redis_fakes.operational.store
    assert "crm.example" not in response.text


def test_a_unit_b_put_leaves_unit_a_in_force(client: TestClient) -> None:
    """The route half of item 193's guard: unit_a's stamps do not move."""
    client.put(UNIT_A_URL, json={"accept_threshold": 77}, headers=_headers())
    before = client.get(UNIT_A_URL, headers=_headers()).json()

    client.put(CALLS_URL, json=ON, headers=_headers())

    after = client.get(UNIT_A_URL, headers=_headers()).json()
    assert after == before
    history = client.get(HISTORY_URL, headers=_headers()).json()["versions"]
    assert history[0][UNIT_B_SECTION]["calls_enabled"] is True
    assert history[0][UNIT_A_SECTION]["accept_threshold"] == 77


def test_the_unit_b_routes_refuse_a_user_token(client: TestClient) -> None:
    user = tokens.mint_token()
    assert client.get(CALLS_URL, headers=_headers(user)).status_code == 401
    assert client.put(CALLS_URL, json=ON, headers=_headers(user)).status_code == 401
