"""Register item 97 (loader in core since F2): each `<tenant>.json` has a section
per unit; Unit A's `unit_a` section is validated into TenantConfig at startup,
over the default, and an invalid one refuses startup naming the tenant."""

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
from dodeal_ai.core.tenant_config import (
    clear_tenant_configs,
    load_tenant_configs,
    tenant_section,
)
from dodeal_ai.main import TENANT_CONFIG_SECTIONS, app
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    EnforcementMode,
    get_tenant_config,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    Band,
    ComponentName,
    NoteType,
)
from dodeal_ai.units.structured_intelligence.scoring import (
    applicable_checks,
    compute_score,
)
from tests.helpers import tokens

DEFAULT = get_tenant_config("tenant-a")


@pytest.fixture(autouse=True)
def _cleared() -> Iterator[None]:
    clear_tenant_configs()
    yield
    clear_tenant_configs()


def _write_file(directory: Path, tenant: str, content: object) -> Path:
    """The whole file: a string as written, anything else as JSON."""
    path = directory / f"{tenant}.json"
    path.write_text(
        content if isinstance(content, str) else json.dumps(content), encoding="utf-8"
    )
    return path


def _write(directory: Path, tenant: str, body: object) -> Path:
    """`body` as the file's `unit_a` section; a string is the whole file."""
    content = body if isinstance(body, str) else {UNIT_A_SECTION: body}
    return _write_file(directory, tenant, content)


def _load(directory: Path) -> None:
    load_tenant_configs(directory, TENANT_CONFIG_SECTIONS)


def _refusal(directory: Path) -> str:
    with pytest.raises(ConfigError) as caught:
        _load(directory)
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
            "short_note_codes": ["NA", "Cb"],
            "band_boundaries": [
                ["poor", 29],
                ["fair", 59],
                ["good", 79],
                ["excellent", 100],
            ],
        },
    )

    _load(tmp_path)

    config = get_tenant_config("tenant-a")
    assert config.config_version == "tenant-a-cfg-1"
    assert (config.accept_threshold, config.flag_threshold) == (75, 40)
    assert config.rate_limit_per_hour == 5
    assert config.enforcement_mode is EnforcementMode.BLOCKING
    assert config.short_note_codes == frozenset({"na", "cb"})  # folded, not as written
    assert config.weights == DEFAULT.weights
    assert config.band_for(60) is Band.GOOD
    assert config.suppressed_components_by_type == DEFAULT.suppressed_components_by_type
    assert get_tenant_config("tenant-b") is DEFAULT


def test_a_minimal_file_is_only_its_version(tmp_path):
    """config_version alone is a valid file: every number stays the default's."""
    _write(tmp_path, "tenant-a", {"config_version": "tenant-a-cfg-2"})
    _load(tmp_path)
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
    _load(tmp_path)
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
    """Every broken file is one ConfigError naming the tenant, never the path.

    Register item 128: a failure INSIDE unit_a's own section parser also names
    the section. `bad-json` fails before any section is even looked at (the
    file itself will not parse), so it stays tenant-only.
    """
    _write(tmp_path, "tenant-a", body)
    expected = (
        "tenant_config_invalid:tenant-a"
        if body == "{not json"
        else "tenant_config_invalid:tenant-a:unit_a"
    )
    assert _refusal(tmp_path) == expected
    assert get_tenant_config("tenant-a") is DEFAULT


def test_one_invalid_file_installs_nothing(tmp_path):
    """All or nothing: a valid tenant is not loaded beside an invalid one."""
    _write(tmp_path, "tenant-a", {"config_version": "a"})
    _write(tmp_path, "tenant-b", {"accept_threshold": 1})
    assert _refusal(tmp_path) == "tenant_config_invalid:tenant-b:unit_a"
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
    _load(tmp_path)
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

    assert str(caught.value) == "tenant_config_invalid:tenant-a:unit_a"
    assert str(tmp_path) not in str(caught.value)
    assert prompting._TEMPLATE_CACHE == {}
    get_settings.cache_clear()


def test_the_route_picks_the_config_by_the_requests_tenant(monkeypatch, tmp_path):
    """tenant-a's request is stamped with its file's version; tenant-b's with the default."""
    _write(tmp_path, "tenant-a", {"config_version": "tenant-a-cfg-1"})
    _load(tmp_path)
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


# --- one file, a section per unit (F2) ------------------------------------------


def test_an_unknown_section_is_ignored(tmp_path):
    """A section no unit parses is skipped, whatever it holds; unit_a still loads."""
    _write_file(
        tmp_path,
        "tenant-a",
        {
            "unit_b": {"anything": [1, "two", None], "config_version": 3},
            UNIT_A_SECTION: {"config_version": "tenant-a-cfg-3"},
        },
    )
    _load(tmp_path)
    assert get_tenant_config("tenant-a").config_version == "tenant-a-cfg-3"
    assert tenant_section("tenant-a", "unit_b") is None


def test_a_missing_unit_a_section_uses_the_default(tmp_path):
    """A file with no unit_a section is valid, and Unit A reads the default."""
    _write_file(tmp_path, "tenant-a", {"unit_b": {"threshold": "not ours"}})
    _load(tmp_path)
    assert get_tenant_config("tenant-a") is DEFAULT
    assert tenant_section("tenant-a", UNIT_A_SECTION) is None


@pytest.mark.parametrize(
    "content,expected",
    [
        (["unit_a"], "tenant_config_invalid:tenant-a"),
        ({UNIT_A_SECTION: 5}, "tenant_config_invalid:tenant-a:unit_a"),
        ({UNIT_A_SECTION: None}, "tenant_config_invalid:tenant-a:unit_a"),
        ("null", "tenant_config_invalid:tenant-a"),
    ],
    ids=["top-level-list", "section-number", "section-null", "json-null"],
)
def test_a_file_or_section_that_is_not_an_object_refuses(tmp_path, content, expected):
    """The file is one object, and Unit A's section is one object. A section
    that fails ITS OWN parse names the section too (register item 128); the
    whole file failing to be an object at all does not, since no section was
    ever reached."""
    _write_file(tmp_path, "tenant-a", content)
    assert _refusal(tmp_path) == expected


def test_core_hands_each_section_to_its_own_parser(tmp_path):
    """The loader knows no schema: a registered parser gets exactly its section."""
    seen: list[object] = []

    def _unit_b(raw: object) -> str:
        seen.append(raw)
        return "parsed-b"

    _write_file(tmp_path, "tenant-a", {"unit_b": {"k": 1}, "unit_c": {"k": 2}})
    load_tenant_configs(tmp_path, {"unit_b": _unit_b})

    assert seen == [{"k": 1}]
    assert tenant_section("tenant-a", "unit_b") == "parsed-b"
    assert tenant_section("tenant-b", "unit_b") is None


def test_a_non_valueerror_from_a_parser_still_refuses_named(tmp_path):
    """Register item 128: the catch is not narrowed to ValueError. A parser
    raising something else entirely -- here TypeError, carrying a value that
    must never reach the message -- still refuses startup naming the tenant
    and the section, never the path and never the value."""
    bad_value = "SENTINEL-bad-value-91fa"

    def _unit_b(raw: object) -> str:
        raise TypeError(f"unexpected shape: {raw}")

    _write_file(tmp_path, "tenant-a", {"unit_b": bad_value})

    with pytest.raises(ConfigError) as caught:
        load_tenant_configs(tmp_path, {"unit_b": _unit_b})

    message = str(caught.value)
    assert message == "tenant_config_invalid:tenant-a:unit_b"
    assert caught.value.__cause__ is None
    assert str(tmp_path) not in message
    assert bad_value not in message


def test_a_file_that_changes_a_weight_is_accepted_and_marks_follow_it(tmp_path):
    """Weights are changeable without a release: the mark table is derived."""
    _write(
        tmp_path,
        "tenant-a",
        {
            "config_version": "tenant-a-cfg-3",
            "weights": {
                "what_happened": 30,
                "client_said": 20,
                "next_step_date": 20,
                "deal_specifics": 20,
                "clarity": 10,
            },
        },
    )
    _load(tmp_path)

    config = get_tenant_config("tenant-a")
    assert config.weights[ComponentName.WHAT_HAPPENED] == 30
    assert config.marks_by_true_count[ComponentName.WHAT_HAPPENED] == (0, 15, 30)
    assert config.marks_by_true_count[ComponentName.NEXT_STEP_DATE] == (0, 10, 20, 20)
    # Untouched components keep the default marks.
    assert (
        config.marks_by_true_count[ComponentName.CLARITY]
        == DEFAULT.marks_by_true_count[ComponentName.CLARITY]
    )
    all_true = dict.fromkeys(applicable_checks(NoteType.DISCOVERY, config), True)
    score = compute_score(all_true, NoteType.DISCOVERY, config)
    assert score.total == 100
    assert [c.mark for c in score.components if not c.suppressed] == [30, 20, 20, 10]
