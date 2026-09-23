"""Runtime tenant rules (register item 97): an administrator's PUT becomes the
rules in force, stamped with a dated version, without a release; resolution is
override, then file, then default, cached per process, and fails safe."""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core import tenant_config
from dodeal_ai.core.breaker import operational_breaker
from dodeal_ai.core.config import get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.core.tenant_config import (
    HISTORY_LIMIT,
    OverrideConflict,
    clear_tenant_configs,
    load_tenant_configs,
    override_key,
    resolve_section,
    set_override,
)
from dodeal_ai.main import TENANT_CONFIG_SECTIONS, app
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import (
    UNIT_A_SECTION,
    get_tenant_config,
    parse_unit_a_section,
    resolve_tenant_config,
    section_of,
)
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.conftest import RedisFakes
from tests.helpers import breakers, tokens
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload

CONFIG_URL = "/api/v1/admin/tenant-config"
HISTORY_URL = "/api/v1/admin/tenant-config/history"
DIRECT = "/api/v1/notes/judgements/direct"
DEFAULT = get_tenant_config("tenant-a")
TODAY = datetime.now(UTC).strftime("%Y%m%d")
FIRST = f"tenant-cfg-tenant-a-{TODAY}-1"


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def client(monkeypatch, llm) -> Iterator[TestClient]:
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    app.dependency_overrides[get_llm_client] = lambda: llm
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def json_log() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_service_token()}",
        "Host": "tenant-a.dodealcrm.com",
    }


def _put(client: TestClient, body: object):
    content = body if isinstance(body, str) else json.dumps(body)
    return client.put(CONFIG_URL, content=content, headers=_headers())


def _judge(client: TestClient, llm: FakeLLM, note_id: int) -> dict:
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
    body = {
        "lead_id": 1656,
        "note_id": note_id,
        "author_id": 27,
        "note_text": "Called the client, discussed the 3BR, following up Tuesday.",
        "lead": {
            "leadType": None,
            "enquiryType": None,
            "project": None,
            "status": None,
        },
    }
    response = client.post(DIRECT, json=body, headers=_headers())
    assert response.status_code == 200
    return response.json()


# --- the guard: a PUT changes the next judgement ----------------------------


def test_a_put_changes_the_next_judgements_thresholds_and_stamp(client, llm) -> None:
    """The version in force stamps the next judgement and its thresholds decide."""
    before = _judge(client, llm, note_id=10)
    total = before["score"]["total"]
    assert before["versions"]["config_version"] == DEFAULT.config_version
    assert before["decision"]["action"] != "accept_silent"

    put = _put(client, {"accept_threshold": total, "flag_threshold": total - 1})
    assert put.status_code == 200
    assert put.json()["version"] == FIRST

    after = _judge(client, llm, note_id=11)
    assert after["versions"]["config_version"] == FIRST
    assert after["decision"]["action"] == "accept_silent"


def test_another_process_sees_the_change_once_its_cache_expires(
    monkeypatch, redis_fakes: RedisFakes
) -> None:
    """A stale cache holds the old rules until tenant_config_cache_seconds pass."""
    clock = [1000.0]
    monkeypatch.setattr(tenant_config.time, "monotonic", lambda: clock[0])
    assert run(resolve_section("tenant-a", UNIT_A_SECTION)).source == "default"

    # Another pod's PUT: the store changes, this process's cache does not.
    record = run(
        set_override("tenant-a", UNIT_A_SECTION, {"rate_limit_per_hour": 9}, now=NOW)
    )
    tenant_config._CACHE["tenant-a"] = tenant_config._CACHE["tenant-a"].__class__(
        fetched_at=clock[0], raw=None, record=None, parsed={}
    )

    clock[0] += get_settings().tenant_config_cache_seconds - 1
    assert run(resolve_section("tenant-a", UNIT_A_SECTION)).source == "default"
    clock[0] += 2
    resolved = run(resolve_section("tenant-a", UNIT_A_SECTION))
    assert (resolved.source, resolved.version) == ("override", record.version)


# --- resolution order and failing safe --------------------------------------


@pytest.fixture
def tenant_file(tmp_path: Path) -> Iterator[Path]:
    """A startup file for tenant-a, loaded as the lifespan would."""
    (tmp_path / "tenant-a.json").write_text(
        json.dumps({UNIT_A_SECTION: {"config_version": "tenant-a-file-1"}}),
        encoding="utf-8",
    )
    load_tenant_configs(tmp_path, TENANT_CONFIG_SECTIONS)
    yield tmp_path
    clear_tenant_configs()
    tenant_config.register_section_parsers(TENANT_CONFIG_SECTIONS)


def test_an_override_wins_over_the_tenant_file(tenant_file) -> None:
    """Override first, then the file: the runtime rule is the one in force."""
    assert run(resolve_tenant_config("tenant-a")).config_version == "tenant-a-file-1"
    run(set_override("tenant-a", UNIT_A_SECTION, {}, now=NOW))
    assert run(resolve_tenant_config("tenant-a")).config_version == FIRST


def test_redis_down_falls_back_to_the_file(
    tenant_file, redis_fakes: RedisFakes, json_log
) -> None:
    """No store and no cache: the file, and one line naming the breaker state."""
    redis_fakes.operational.raise_on.add("get")
    resolved = run(resolve_section("tenant-a", UNIT_A_SECTION))
    assert (resolved.source, resolved.value.config_version) == (  # type: ignore[union-attr]
        "file",
        "tenant-a-file-1",
    )
    line = next(
        x
        for x in _lines(json_log)
        if x["message"] == "tenant_config_override_unavailable"
    )
    assert (line["tenant"], line["reason_code"]) == (
        "tenant-a",
        "tenant_config_override_unavailable",
    )


def test_an_open_breaker_is_named_on_the_fallback_line(tenant_file, json_log) -> None:
    """The breaker field says the store was not even asked."""
    run(breakers.trip(operational_breaker()))
    assert run(resolve_section("tenant-a", UNIT_A_SECTION)).source == "file"
    line = next(
        x
        for x in _lines(json_log)
        if x["message"] == "tenant_config_override_unavailable"
    )
    assert line["breaker"] == "open"


def test_redis_down_after_a_read_keeps_the_last_override(
    monkeypatch, redis_fakes: RedisFakes
) -> None:
    """A stale cached override beats the file when the store is gone."""
    clock = [1000.0]
    monkeypatch.setattr(tenant_config.time, "monotonic", lambda: clock[0])
    run(set_override("tenant-a", UNIT_A_SECTION, {}, now=NOW))
    clock[0] += 3600
    redis_fakes.operational.raise_on.add("get")
    assert run(resolve_tenant_config("tenant-a")).config_version == FIRST


def test_redis_down_with_no_file_is_the_safe_default(redis_fakes: RedisFakes) -> None:
    """Advisory, the default version: never a guess."""
    redis_fakes.operational.raise_on.add("get")
    config = run(resolve_tenant_config("tenant-a"))
    assert config == DEFAULT


def test_a_corrupt_stored_record_is_ignored_and_logged(
    redis_fakes: RedisFakes, json_log
) -> None:
    """Not JSON, missing keys, or a section today's parser refuses: all ignored."""
    for raw in (
        "not json",
        json.dumps({"version": "v"}),
        json.dumps(
            {"version": "v", "set_at": "t", "sections": {UNIT_A_SECTION: {"x": 1}}}
        ),
    ):
        tenant_config.reset_override_cache()
        redis_fakes.operational.store[override_key("tenant-a")] = raw
        assert run(resolve_section("tenant-a", UNIT_A_SECTION)).source == "default"
    lines = [
        x for x in _lines(json_log) if x["message"] == "tenant_config_override_invalid"
    ]
    assert len(lines) == 3
    assert "not json" not in json.dumps(lines)


# --- the admin routes -------------------------------------------------------


def test_get_shows_the_rules_in_force_with_their_version_and_source(client) -> None:
    """Default first; after a PUT the override, its set_at and its values."""
    first = client.get(CONFIG_URL, headers=_headers()).json()
    assert (first["version"], first["source"], first["set_at"]) == (
        DEFAULT.config_version,
        "default",
        None,
    )
    assert first[UNIT_A_SECTION]["accept_threshold"] == DEFAULT.accept_threshold

    _put(client, {"accept_threshold": 80})
    now = client.get(CONFIG_URL, headers=_headers()).json()
    assert (now["version"], now["source"]) == (FIRST, "override")
    assert now["set_at"].startswith(datetime.now(UTC).strftime("%Y-%m-%d"))
    assert now[UNIT_A_SECTION]["accept_threshold"] == 80


def test_versions_count_up_within_a_day_and_history_is_newest_first(client) -> None:
    """-1, -2, -3; the history lists them in reverse."""
    for threshold in (71, 72, 73):
        assert _put(client, {"accept_threshold": threshold}).status_code == 200
    history = client.get(HISTORY_URL, headers=_headers()).json()["versions"]
    assert [entry["version"] for entry in history] == [
        f"tenant-cfg-tenant-a-{TODAY}-{n}" for n in (3, 2, 1)
    ]
    assert history[0][UNIT_A_SECTION]["accept_threshold"] == 73


def test_a_new_day_starts_the_count_again(redis_fakes: RedisFakes) -> None:
    """The date in the version is the day it was set, in UTC. Both instants
    are fixed: the test must not depend on the day it is run."""
    first = datetime(2026, 9, 22, 23, 30, tzinfo=UTC)
    earlier = run(set_override("tenant-a", UNIT_A_SECTION, {}, now=first))
    assert earlier.version == "tenant-cfg-tenant-a-20260922-1"
    later = datetime(2026, 9, 23, 0, 30, tzinfo=UTC)
    record = run(set_override("tenant-a", UNIT_A_SECTION, {}, now=later))
    assert record.version == "tenant-cfg-tenant-a-20260923-1"


def test_the_history_keeps_at_most_fifty(redis_fakes: RedisFakes) -> None:
    """The capped list drops the oldest."""
    for _ in range(HISTORY_LIMIT + 1):
        run(set_override("tenant-a", UNIT_A_SECTION, {}, now=NOW))
    history = run(tenant_config.override_history("tenant-a"))
    assert len(history) == HISTORY_LIMIT
    assert history[0].version.endswith(f"-{HISTORY_LIMIT + 1}")


@pytest.mark.parametrize(
    "body",
    [{"accept_threshold": 500}, {"no_such_rule": 1}, [1, 2], "not json", '"a"'],
)
def test_an_invalid_body_is_422_and_leaves_the_version_in_force(
    client, body, json_log
) -> None:
    """Refused whole, naming nothing, and the rules in force do not move."""
    _put(client, {"accept_threshold": 75})
    refused = _put(client, body)

    assert refused.status_code == 422
    assert refused.json()["reason"] == "invalid_tenant_config"
    assert "accept_threshold" not in refused.text and "no_such_rule" not in refused.text
    assert client.get(CONFIG_URL, headers=_headers()).json()["version"] == FIRST
    assert "no_such_rule" not in json_log.getvalue()


def test_a_client_version_is_replaced_by_the_dated_one(client) -> None:
    """config_version is the service's to set, never the caller's."""
    put = _put(client, {"config_version": "mine", "accept_threshold": 75})
    assert put.json()["version"] == FIRST
    assert client.get(CONFIG_URL, headers=_headers()).json()["version"] == FIRST


def test_the_change_is_one_info_line_with_tenant_and_version(client, json_log) -> None:
    """tenant_config_changed carries the tenant and the version, no values."""
    _put(client, {"accept_threshold": 77})
    line = next(x for x in _lines(json_log) if x["message"] == "tenant_config_changed")
    assert (line["level"], line["tenant"], line["version"]) == (
        "INFO",
        "tenant-a",
        FIRST,
    )
    assert "accept_threshold" not in json.dumps(line)


def test_a_dead_store_is_503_on_put_and_history(client, redis_fakes: RedisFakes):
    """Writes and the history refuse; judgements are unaffected (tested above)."""
    redis_fakes.operational.raise_on.add("get")
    assert _put(client, {}).json()["reason"] == "tenant_config_unavailable"
    redis_fakes.operational.raise_on = {"eval", "lrange"}
    assert _put(client, {}).status_code == 503
    history = client.get(HISTORY_URL, headers=_headers())
    assert (history.status_code, history.json()["reason"]) == (
        503,
        "tenant_config_unavailable",
    )


def test_a_lost_race_is_retried_and_then_409(client, monkeypatch) -> None:
    """Three compare-and-sets lost in a row: 409, and nothing half-written."""

    async def _lose(*_args: object) -> bool:
        return False

    monkeypatch.setattr(tenant_config, "_write", _lose)
    r = _put(client, {})
    assert (r.status_code, r.json()["reason"]) == (409, "tenant_config_conflict")
    with pytest.raises(OverrideConflict):
        run(set_override("tenant-a", UNIT_A_SECTION, {}, now=NOW))


def test_the_admin_routes_refuse_a_user_token(client) -> None:
    """The service chain only."""
    user = {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }
    assert client.get(CONFIG_URL, headers=user).status_code == 401
    assert client.put(CONFIG_URL, content="{}", headers=user).status_code == 401
    assert client.get(HISTORY_URL, headers=user).status_code == 401


def test_versions_reads_the_resolved_stamp(client) -> None:
    """/meta/versions reports the version in force, not the file's."""
    _put(client, {})
    body = client.get("/api/v1/meta/versions", headers=_headers()).json()
    assert body["config_version"] == FIRST


def test_a_config_round_trips_through_its_section() -> None:
    """section_of is what the admin screen shows; parsing it gives it back."""
    assert parse_unit_a_section(section_of(DEFAULT)) == DEFAULT


# --- helpers ----------------------------------------------------------------

NOW = datetime.now(UTC)


def run(coro):
    """Drive one coroutine to completion from a sync test."""
    import asyncio

    return asyncio.run(coro)
