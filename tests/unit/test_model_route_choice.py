"""Each company picks its model route (unit_a and unit_b `model_route`) and its
STT profile (unit_b `stt_profile`) at runtime, through the admin PUT: an
unknown name is 422, and a change moves policy_version, not config_version.

The guard: tenant A on route "device" and tenant B on "default" reach
different providers in the same process."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import ConfigError, Settings, get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.llm.routing import ModelRouter, RoutedClient
from dodeal_ai.core.tenant_config import load_tenant_configs, set_override
from dodeal_ai.main import TENANT_CONFIG_SECTIONS, app
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.call_intelligence.config import (
    UNIT_B_SECTION,
    CallsConfig,
    new_config_version,
    parse_unit_b_section,
)
from dodeal_ai.units.call_intelligence.worker import tenant_client
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    kept_config_version,
    parse_unit_a_section,
)
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload

NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)
ADMIN = "/api/v1/admin/tenant-config"
DIRECT = "/api/v1/notes/judgements/direct"
NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."
PASSES = ("unit_a.classify", "unit_a.vague", "unit_a.score")
OWNER = {
    "kind": "openai_compatible",
    "base_url": "https://owner.example.test/v1",
    "api_key_env": "OWNER_TEST_LLM_KEY",
    "timeout_seconds": 30,
}
WHISPER = {
    "provider": "openai_compatible",
    "base_url": "https://owner.example.test/stt/v1",
    "model": "whisper-large-v3",
    "api_key_env": "OWNER_TEST_STT_KEY",
}


@pytest.fixture(autouse=True)
def _routes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """A deployment with the route "device" and the STT profile "whisper"."""
    profiles = {
        f"owner.{name}": {"provider": "owner", "model": "owner-model"}
        for name in PASSES
    }
    for name, value in {
        "LLM_PROVIDERS": json.dumps({"owner": OWNER}),
        "LLM_PROFILES": json.dumps(profiles),
        "MODEL_ROUTES": json.dumps(
            {"device": {name: f"owner.{name}" for name in PASSES}}
        ),
        "CALL_STT_PROFILES": json.dumps({"whisper": WHISPER}),
    }.items():
        monkeypatch.setenv(f"DODEAL_{name}", value)
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


# --- the sections accept only names this deployment has ---------------------


def test_each_section_defaults_to_the_default_route_and_profile() -> None:
    config = CallsConfig()
    assert (config.model_route, config.stt_profile) == ("default", "default")
    assert parse_unit_a_section({"config_version": "v"}).model_route == "default"


@pytest.mark.parametrize(
    ("section", "body"),
    [
        (UNIT_A_SECTION, {"config_version": "v", "model_route": "nowhere"}),
        (UNIT_B_SECTION, {"model_route": "nowhere"}),
        (UNIT_B_SECTION, {"stt_profile": "nowhere"}),
    ],
)
def test_an_unknown_route_or_profile_is_refused(section: str, body: dict) -> None:
    """The parser refuses; its callers re-raise with fixed text only."""
    with pytest.raises(ValueError):
        TENANT_CONFIG_SECTIONS[section](body)


def test_a_tenant_file_naming_an_unknown_route_refuses_startup(tmp_path: Path) -> None:
    (tmp_path / "tenant-a.json").write_text(
        json.dumps({"unit_a": {"config_version": "v1", "model_route": "nowhere"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="^tenant_config_invalid:tenant-a:unit_a$"):
        load_tenant_configs(tmp_path, TENANT_CONFIG_SECTIONS)


# --- a routing change is policy, not a new config_version --------------------


async def test_a_unit_a_route_change_moves_policy_version_only() -> None:
    first = await set_override(
        "tenant-a", UNIT_A_SECTION, {}, now=NOW, keep_version=kept_config_version
    )
    moved = await set_override(
        "tenant-a",
        UNIT_A_SECTION,
        {"model_route": "device"},
        now=LATER,
        keep_version=kept_config_version,
    )
    assert moved.stamp_for(UNIT_A_SECTION).version == first.version
    assert moved.policy_version != first.policy_version
    assert moved.sections[UNIT_A_SECTION]["model_route"] == "device"


@pytest.mark.parametrize(
    "change", [{"model_route": "device"}, {"stt_profile": "whisper"}]
)
async def test_a_unit_b_routing_change_moves_policy_version_only(
    change: dict,
) -> None:
    first = await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {"min_transcribe_seconds": 20},
        now=NOW,
        keep_version=new_config_version,
    )
    moved = await set_override(
        "tenant-a",
        UNIT_B_SECTION,
        {"min_transcribe_seconds": 20, **change},
        now=LATER,
        keep_version=new_config_version,
    )
    assert moved.stamp_for(UNIT_B_SECTION).version == first.version
    assert moved.policy_version != first.policy_version


def test_a_unit_b_rule_change_keeps_config_version_too() -> None:
    """Routing or rules, or nothing at all: the one in force (F-8). A first
    one is minted only while none is in force."""
    in_force = parse_unit_b_section({"config_version": "cfg-1"})
    assert new_config_version({"model_route": "device"}, in_force) == "cfg-1"
    assert (
        new_config_version(
            {"model_route": "device", "min_transcribe_seconds": 9}, in_force
        )
        == "cfg-1"
    )
    assert new_config_version({}, in_force) == "cfg-1"
    assert new_config_version({"model_route": "device"}, None) is None
    assert new_config_version({"model_route": "device"}, CallsConfig()) is None


# --- the guard: two tenants, two providers, one process --------------------


def _script(llm: FakeLLM) -> None:
    """One judgement's three answers, by template."""
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "discovery"}))
    llm.script_for(
        template_for(NoteType.DISCOVERY),
        json_response(
            {
                "is_vague": False,
                "missing_components": [],
                "clarification_prompt": None,
                "reasoning": "Complete.",
            }
        ),
    )
    llm.script_for(SCORE_TEMPLATE, json_response(score_payload()))


def _service(tenant: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token(subdomain=tenant)}",
        "Host": f"{tenant}.dodealcrm.com",
    }


def test_two_tenants_on_two_routes_reach_two_providers(
    monkeypatch: pytest.MonkeyPatch, _routes: Settings
) -> None:
    """tenant-a moves to "device" through the admin PUT; tenant-b stays on
    "default". Each judgement's three passes reach its own route's provider."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    settings = get_settings()
    api, owner = FakeLLM(), FakeLLM()
    routes = {
        name: RoutedClient(
            name, passes, settings=settings, default=api, providers={"owner": owner}
        )
        for name, passes in {"default": {}, **settings.model_routes}.items()
    }
    router = ModelRouter(routes, {"owner": owner}, [])  # type: ignore[dict-item]
    verifier = JwtVerifier(
        Settings(
            _env_file=None,
            jwt_signing_key=tokens.TEST_SECRET,
            jwt_algorithm=tokens.TEST_ALG,
        )
    )
    app.dependency_overrides[get_verifier] = lambda: verifier
    app.dependency_overrides[get_leads_client] = lambda: None
    app.dependency_overrides[get_llm_client] = lambda: router
    _script(api)
    _script(owner)
    body = {
        "lead_id": 1656,
        "note_id": 10,
        "author_id": 27,
        "note_text": NOTE,
        "lead": {
            "leadType": "buyer",
            "enquiryType": "sale",
            "project": None,
            "status": "warm",
        },
    }
    try:
        client = TestClient(app)
        moved = client.put(
            ADMIN, json={"model_route": "device"}, headers=_service("tenant-a")
        )
        assert moved.status_code == 200
        refused = client.put(
            ADMIN, json={"model_route": "nowhere"}, headers=_service("tenant-b")
        )
        assert (refused.status_code, refused.json()["reason"]) == (
            422,
            "invalid_tenant_config",
        )

        for tenant in ("tenant-a", "tenant-b"):
            judged = client.post(DIRECT, json=body, headers=_service(tenant))
            assert judged.status_code == 200, judged.text
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert sorted(owner.profiles) == sorted(f"owner.{name}" for name in PASSES)
    assert sorted(api.profiles) == sorted(PASSES)
    assert "nowhere" not in refused.text


def test_a_call_worker_sends_through_the_tenants_route(_routes: Settings) -> None:
    fake = FakeLLM()
    routes = {
        "default": RoutedClient(
            "default", {}, settings=_routes, default=fake, providers={}
        )
    }
    router = ModelRouter(routes, {}, [])
    assert tenant_client({}, CallsConfig()) is None
    assert tenant_client({"llm": router}, CallsConfig()) is routes["default"]
    device = CallsConfig(model_route="device")
    assert tenant_client({"llm": fake}, device) is not fake
