"""Register item 97: `<tenant>.json` files validated into TenantConfig at
startup, over the default; an invalid one refuses startup naming the tenant."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core import prompting
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import ConfigError, Settings, get_settings
from dodeal_ai.main import app
from dodeal_ai.units.structured_intelligence.config import (
    EnforcementMode,
    clear_tenant_configs,
    get_tenant_config,
    load_tenant_configs,
)
from dodeal_ai.units.structured_intelligence.schemas import Band, ComponentName
from tests.helpers import tokens

DEFAULT = get_tenant_config("tenant-a")


@pytest.fixture(autouse=True)
def _cleared() -> Iterator[None]:
    clear_tenant_configs()
    yield
    clear_tenant_configs()


def _write(directory: Path, tenant: str, body: object) -> Path:
    path = directory / f"{tenant}.json"
    path.write_text(
        body if isinstance(body, str) else json.dumps(body), encoding="utf-8"
    )
    return path


def _refusal(directory: Path) -> str:
    with pytest.raises(ConfigError) as caught:
        load_tenant_configs(directory)
    assert caught.value.__cause__ is None
    assert str(directory) not in str(caught.value)
    return str(caught.value)


def test_a_valid_file_overrides_the_default_for_its_tenant_only(tmp_path):
    """tenant-a gets its file over the default; tenant-b keeps the default."""
    _write(
        tmp_path,
        "tenant-a",
        {
            "config_version": "tenant-a-cfg-1",
            "accept_threshold": 75,
            "rate_limit_per_hour": 5,
            "enforcement_mode": "blocking",
            "weights": {
                "what_happened": 30,
                "client_said": 20,
                "next_step_date": 20,
                "deal_specifics": 20,
                "clarity": 10,
            },
            "band_boundaries": [
                ["poor", 29],
                ["fair", 59],
                ["good", 79],
                ["excellent", 100],
            ],
        },
    )

    load_tenant_configs(tmp_path)

    config = get_tenant_config("tenant-a")
    assert config.config_version == "tenant-a-cfg-1"
    assert (config.accept_threshold, config.flag_threshold) == (75, 40)
    assert config.rate_limit_per_hour == 5
    assert config.enforcement_mode is EnforcementMode.BLOCKING
    assert config.weights[ComponentName.WHAT_HAPPENED] == 30
    assert config.band_for(60) is Band.GOOD
    assert config.suppressed_components_by_type == DEFAULT.suppressed_components_by_type
    assert get_tenant_config("tenant-b") is DEFAULT


def test_a_minimal_file_is_only_its_version(tmp_path):
    """config_version alone is a valid file: every number stays the default's."""
    _write(tmp_path, "tenant-a", {"config_version": "tenant-a-cfg-2"})
    load_tenant_configs(tmp_path)
    config = get_tenant_config("tenant-a")
    assert config.config_version == "tenant-a-cfg-2"
    assert config.weights == DEFAULT.weights


def test_the_weights_stay_immutable(tmp_path):
    """A loaded tenant's containers are as read-only as the default's."""
    _write(
        tmp_path,
        "tenant-a",
        {"config_version": "v", "weights": dict(DEFAULT.weights)},
    )
    load_tenant_configs(tmp_path)
    with pytest.raises(TypeError):
        get_tenant_config("tenant-a").weights[ComponentName.CLARITY] = 0  # type: ignore[index]


@pytest.mark.parametrize(
    "body",
    [
        {"accept_threshold": 75},
        {"config_version": ""},
        {"config_version": "v", "unknown_field": 1},
        {"config_version": "v", "deal_specifics_applicable": True},
        {"config_version": "v", "accept_threshold": 101},
        {"config_version": "v", "max_note_chars": 4001},
        {"config_version": "v", "idempotency_ttl_seconds": 0},
        {"config_version": "v", "weights": {"what_happened": 100}},
        {
            "config_version": "v",
            "weights": {**{k.value: 20 for k in ComponentName}, "clarity": 30},
        },
        {
            "config_version": "v",
            "weights": {**{k.value: 20 for k in ComponentName}, "clarity": 0},
        },
        {
            "config_version": "v",
            "weights": {**{k.value: 30 for k in ComponentName}, "clarity": -20},
        },
        {
            "config_version": "v",
            "band_boundaries": [
                ["fair", 39],
                ["poor", 69],
                ["good", 84],
                ["excellent", 100],
            ],
        },
        {
            "config_version": "v",
            "band_boundaries": [
                ["poor", 69],
                ["fair", 39],
                ["good", 84],
                ["excellent", 100],
            ],
        },
        {
            "config_version": "v",
            "band_boundaries": [
                ["poor", 39],
                ["fair", 69],
                ["good", 84],
                ["excellent", 99],
            ],
        },
        {
            "config_version": "v",
            "band_boundaries": [
                ["poor", -1],
                ["fair", 69],
                ["good", 84],
                ["excellent", 100],
            ],
        },
        {"config_version": "v", "flag_threshold": 70},
        {"config_version": "v", "min_note_chars": 2000},
        "{not json",
    ],
    ids=[
        "no-version",
        "empty-version",
        "extra-field",
        "q13-switch",
        "threshold-range",
        "note-cap",
        "zero-ttl",
        "weights-incomplete",
        "weights-sum",
        "weights-sum-low",
        "weights-negative",
        "bands-order",
        "bands-descending",
        "bands-top",
        "bands-negative",
        "flag-not-below-accept",
        "min-not-below-max",
        "bad-json",
    ],
)
def test_an_invalid_file_refuses_naming_the_tenant(tmp_path, body):
    """Every broken file is one ConfigError naming the tenant, never the path."""
    _write(tmp_path, "tenant-a", body)
    assert _refusal(tmp_path) == "tenant_config_invalid:tenant-a"
    assert get_tenant_config("tenant-a") is DEFAULT


def test_one_invalid_file_installs_nothing(tmp_path):
    """All or nothing: a valid tenant is not loaded beside an invalid one."""
    _write(tmp_path, "tenant-a", {"config_version": "a"})
    _write(tmp_path, "tenant-b", {"accept_threshold": 1})
    assert _refusal(tmp_path) == "tenant_config_invalid:tenant-b"
    assert get_tenant_config("tenant-a") is DEFAULT


def test_an_unreadable_file_refuses_naming_the_tenant(tmp_path):
    """A `<tenant>.json` that cannot be read is the same refusal."""
    (tmp_path / "tenant-a.json").mkdir()
    assert _refusal(tmp_path) == "tenant_config_invalid:tenant-a"


@pytest.mark.parametrize("stem", ["Tenant-A", "tenant_a", "-tenant"])
def test_a_file_name_that_is_not_a_tenant_label_refuses(tmp_path, stem):
    """The file name is the tenant, so it must be a valid tenant label."""
    _write(tmp_path, stem, {"config_version": "v"})
    assert _refusal(tmp_path) == "tenant_config_invalid_name"


def test_a_missing_directory_refuses_without_its_path(tmp_path):
    """A configured directory that is not there is a refusal, not the default."""
    assert _refusal(tmp_path / "absent") == "tenant_config_dir_unreadable"


def test_other_files_are_ignored(tmp_path):
    """Only `*.json` is a tenant file."""
    (tmp_path / "README.txt").write_text("not a config", encoding="utf-8")
    load_tenant_configs(tmp_path)
    assert get_tenant_config("tenant-a") is DEFAULT


# --- startup and the routes -------------------------------------------------


def _env(monkeypatch, directory: Path) -> None:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    monkeypatch.setenv("DODEAL_TENANT_CONFIG_DIR", str(directory))
    get_settings.cache_clear()


def test_startup_loads_the_files_and_shutdown_clears_them(monkeypatch, tmp_path):
    """Loaded for the app's life, gone after it."""
    _write(tmp_path, "tenant-a", {"config_version": "tenant-a-cfg-1"})
    _env(monkeypatch, tmp_path)

    with TestClient(app):
        assert get_tenant_config("tenant-a").config_version == "tenant-a-cfg-1"

    assert get_tenant_config("tenant-a") is DEFAULT
    get_settings.cache_clear()


def test_an_invalid_file_refuses_startup_and_leaves_no_templates(monkeypatch, tmp_path):
    """The refusal names the tenant, and the preloaded templates are released."""
    _write(tmp_path, "tenant-a", {"accept_threshold": 75})
    _env(monkeypatch, tmp_path)

    with pytest.raises(ConfigError) as caught, TestClient(app):
        pass

    assert str(caught.value) == "tenant_config_invalid:tenant-a"
    assert str(tmp_path) not in str(caught.value)
    assert prompting._TEMPLATE_CACHE == {}
    get_settings.cache_clear()


def test_the_route_picks_the_config_by_the_requests_tenant(monkeypatch, tmp_path):
    """tenant-a's request is stamped with its file's version; tenant-b's with the default."""
    _write(tmp_path, "tenant-a", {"config_version": "tenant-a-cfg-1"})
    load_tenant_configs(tmp_path)
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    verifier = JwtVerifier(
        Settings(
            _env_file=None,
            jwt_signing_key=tokens.TEST_SECRET,
            jwt_algorithm=tokens.TEST_ALG,
        )
    )
    app.dependency_overrides[get_verifier] = lambda: verifier
    try:
        client = TestClient(app)
        versions = {}
        for tenant in ("tenant-a", "tenant-b"):
            token = tokens.mint_token(subdomain=tenant, sub=42)
            response = client.get(
                "/api/v1/meta/versions",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Host": f"{tenant}.dodealcrm.com",
                },
            )
            assert response.status_code == 200
            versions[tenant] = response.json()["config_version"]
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()

    assert versions == {
        "tenant-a": "tenant-a-cfg-1",
        "tenant-b": DEFAULT.config_version,
    }
