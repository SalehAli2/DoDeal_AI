"""Runtime tenant rules (register item 97): an administrator's PUT becomes the
rules in force, with a dated policy_version and a config_version that moves only
on a mark-affecting change; resolution is override, file, default, and safe."""

from __future__ import annotations

import dataclasses
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
    MARK_AFFECTING_FIELDS,
    UNIT_A_SECTION,
    TenantConfig,
    get_tenant_config,
    kept_config_version,
    parse_unit_a_section,
    resolve_tenant_config,
    section_of,
)
from dodeal_ai.units.structured_intelligence.judgement_rows import JudgementRow
from dodeal_ai.units.structured_intelligence.measures import average_band
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
POLICY = f"tenant-policy-tenant-a-{TODAY}-"
# A rubric other than the default's: a mark-affecting change.
OTHER_WEIGHTS = {
    "what_happened": 30,
    "client_said": 15,
    "next_step_date": 25,
    "deal_specifics": 20,
    "clarity": 10,
}


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


def _set(body: dict, now: datetime | None = None):
    """set_override as the admin PUT calls it, keeper included."""
    return set_override(
        "tenant-a",
        UNIT_A_SECTION,
        body,
        now=now or NOW,
        keep_version=kept_config_version,
    )


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
    """The PUT's policy_version stamps the next judgement and its thresholds
    decide; a threshold marks nothing, so the config_version stays."""
    before = _judge(client, llm, note_id=10)
    total = before["score"]["total"]
    assert before["versions"]["config_version"] == DEFAULT.config_version
    assert before["versions"]["policy_version"] is None
    assert before["decision"]["action"] != "accept_silent"

    put = _put(client, {"accept_threshold": total, "flag_threshold": total - 1})
    assert put.status_code == 200
    assert put.json()["version"] == DEFAULT.config_version
    assert put.json()["policy_version"] == f"{POLICY}1"

    after = _judge(client, llm, note_id=11)
    assert after["versions"]["config_version"] == DEFAULT.config_version
    assert after["versions"]["policy_version"] == f"{POLICY}1"
    assert after["decision"]["action"] == "accept_silent"


def test_another_process_sees_the_change_once_its_cache_expires(
    monkeypatch, redis_fakes: RedisFakes
) -> None:
    """A stale cache holds the old rules until tenant_config_cache_seconds pass."""
    clock = [1000.0]
    monkeypatch.setattr(tenant_config.time, "monotonic", lambda: clock[0])
    assert run(resolve_section("tenant-a", UNIT_A_SECTION)).source == "default"

    # Another pod's PUT: the store changes, this process's cache does not.
    record = run(_set({"rate_limit_per_hour": 9}))
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


def _stamps(config: TenantConfig) -> tuple[str, str | None]:
    return config.config_version, config.policy_version


def test_an_override_wins_over_the_tenant_file(tenant_file) -> None:
    """Override first, then the file: the runtime rule is the one in force, and
    a PUT that marks as the file does keeps the file's config_version."""
    assert _stamps(run(resolve_tenant_config("tenant-a"))) == ("tenant-a-file-1", None)
    run(_set({"enforcement_mode": "off"}))
    config = run(resolve_tenant_config("tenant-a"))
    assert _stamps(config) == ("tenant-a-file-1", f"{POLICY}1")
    assert config.enforcement_mode == "off"


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
    run(_set({"weights": OTHER_WEIGHTS}))
    clock[0] += 3600
    redis_fakes.operational.raise_on.add("get")
    assert _stamps(run(resolve_tenant_config("tenant-a"))) == (FIRST, f"{POLICY}1")


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
    assert (
        first["version"],
        first["policy_version"],
        first["source"],
        first["set_at"],
    ) == (DEFAULT.config_version, None, "default", None)
    assert first[UNIT_A_SECTION]["accept_threshold"] == DEFAULT.accept_threshold

    _put(client, {"accept_threshold": 80})
    now = client.get(CONFIG_URL, headers=_headers()).json()
    assert (now["version"], now["policy_version"], now["source"]) == (
        DEFAULT.config_version,
        f"{POLICY}1",
        "override",
    )
    assert now["set_at"].startswith(datetime.now(UTC).strftime("%Y-%m-%d"))
    assert now[UNIT_A_SECTION]["accept_threshold"] == 80


def test_versions_count_up_within_a_day_and_history_is_newest_first(client) -> None:
    """policy -1, -2, -3; the history lists them in reverse, each with the
    config_version in force, which three thresholds never moved."""
    for threshold in (71, 72, 73):
        assert _put(client, {"accept_threshold": threshold}).status_code == 200
    history = client.get(HISTORY_URL, headers=_headers()).json()["versions"]
    assert [entry["policy_version"] for entry in history] == [
        f"{POLICY}{n}" for n in (3, 2, 1)
    ]
    assert {entry["version"] for entry in history} == {DEFAULT.config_version}
    assert history[0][UNIT_A_SECTION]["accept_threshold"] == 73


def test_a_new_day_starts_the_count_again(redis_fakes: RedisFakes) -> None:
    """The date in both stamps is the day it was set, in UTC. Both instants
    are fixed: the test must not depend on the day it is run."""
    first = datetime(2026, 9, 22, 23, 30, tzinfo=UTC)
    earlier = run(_set({"weights": OTHER_WEIGHTS}, now=first))
    assert (earlier.version, earlier.policy_version) == (
        "tenant-cfg-tenant-a-20260922-1",
        "tenant-policy-tenant-a-20260922-1",
    )
    later = datetime(2026, 9, 23, 0, 30, tzinfo=UTC)
    record = run(_set({}, now=later))
    assert (record.version, record.policy_version) == (
        "tenant-cfg-tenant-a-20260923-1",
        "tenant-policy-tenant-a-20260923-1",
    )


def test_the_history_keeps_at_most_fifty(redis_fakes: RedisFakes) -> None:
    """The capped list drops the oldest."""
    for _ in range(HISTORY_LIMIT + 1):
        run(_set({}))
    history = run(tenant_config.override_history("tenant-a"))
    assert len(history) == HISTORY_LIMIT
    assert history[0].policy_version == f"{POLICY}{HISTORY_LIMIT + 1}"


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
    in_force = client.get(CONFIG_URL, headers=_headers()).json()
    assert in_force["policy_version"] == f"{POLICY}1"
    assert "no_such_rule" not in json_log.getvalue()


def test_a_client_version_is_replaced_by_the_dated_one(client) -> None:
    """config_version is the service's to set, never the caller's: kept on a
    change that marks nothing, the next dated one on a change that does."""
    put = _put(client, {"config_version": "mine", "accept_threshold": 75})
    assert put.json()["version"] == DEFAULT.config_version
    put = _put(client, {"config_version": "mine", "weights": OTHER_WEIGHTS})
    assert put.json()["version"] == FIRST
    assert client.get(CONFIG_URL, headers=_headers()).json()["version"] == FIRST


def test_the_change_is_one_info_line_with_tenant_and_version(client, json_log) -> None:
    """tenant_config_changed carries the tenant and both stamps, no values."""
    _put(client, {"accept_threshold": 77})
    line = next(x for x in _lines(json_log) if x["message"] == "tenant_config_changed")
    assert (
        line["level"],
        line["tenant"],
        line["version"],
        line["policy_version"],
    ) == ("INFO", "tenant-a", DEFAULT.config_version, f"{POLICY}1")
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
        run(_set({}))


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
    """/meta/versions reports both stamps in force, not the default's."""
    _put(client, {"weights": OTHER_WEIGHTS})
    body = client.get("/api/v1/meta/versions", headers=_headers()).json()
    assert (body["config_version"], body["policy_version"]) == (FIRST, f"{POLICY}1")


def test_a_config_round_trips_through_its_section() -> None:
    """section_of is what the admin screen shows; parsing it gives it back."""
    assert parse_unit_a_section(section_of(DEFAULT)) == DEFAULT


# --- two stamps (register item 97) ------------------------------------------


def test_a_mode_only_put_keeps_config_version_and_moves_policy_version(
    client,
) -> None:
    """The guard: switching the mode marks nothing, so only the policy moves."""
    for n, mode in enumerate(("off", "strict"), start=1):
        put = _put(client, {"enforcement_mode": mode})
        assert (put.json()["version"], put.json()["policy_version"]) == (
            DEFAULT.config_version,
            f"{POLICY}{n}",
        )


@pytest.mark.parametrize(
    "change",
    [
        {"weights": OTHER_WEIGHTS},
        {
            "band_boundaries": [
                ["poor", 49],
                ["fair", 69],
                ["good", 84],
                ["excellent", 100],
            ]
        },
        {"deal_specifics_applicable": True},
    ],
)
def test_a_mark_affecting_put_moves_both_stamps(client, change: dict) -> None:
    """The guard: a weight, a band boundary or the Q13 switch is a new rubric."""
    put = _put(client, change)
    assert (put.json()["version"], put.json()["policy_version"]) == (
        FIRST,
        f"{POLICY}1",
    )


def test_a_put_that_leaves_the_weights_out_puts_them_back(client) -> None:
    """PUT replaces the whole section: GET first, or the default returns."""
    _put(client, {"weights": OTHER_WEIGHTS})
    section = client.get(CONFIG_URL, headers=_headers()).json()[UNIT_A_SECTION]
    kept = _put(client, {**section, "enforcement_mode": "off"}).json()
    assert (kept["version"], kept["policy_version"]) == (FIRST, f"{POLICY}2")
    dropped = _put(client, {"enforcement_mode": "strict"}).json()
    assert (dropped["version"], dropped["policy_version"]) == (
        f"tenant-cfg-tenant-a-{TODAY}-2",
        f"{POLICY}3",
    )


def _row(note_id: int, judgement: dict) -> JudgementRow:
    """A scored row as the CRM would store this judgement."""
    return JudgementRow.model_validate(
        {
            "note_id": note_id,
            "lead_id": judgement["lead_id"],
            "author_id": judgement["author_id"],
            "note_created_at": NOW,
            "note_type": judgement["analysis"]["note_type"],
            "band": judgement["score"]["band"],
            "total": judgement["score"]["total"],
            "denominator": judgement["score"]["denominator"],
            "prompt_sent": judgement["decision"]["prompt_sent"],
            "enforcement_verdict": judgement["enforcement"]["verdict"],
            **judgement["versions"],
        }
    )


def test_the_average_band_stays_computed_across_a_mode_only_change(client, llm) -> None:
    """The guard: rows either side of a mode-only PUT are one comparable set."""
    before = _judge(client, llm, note_id=20)
    _put(client, {"enforcement_mode": "off"})
    after = _judge(client, llm, note_id=21)
    assert after["versions"]["policy_version"] == f"{POLICY}1"

    rows = [_row(n, before) for n in range(1, 6)] + [
        _row(n, after) for n in range(6, 11)
    ]
    measure = average_band(rows, DEFAULT)
    assert (measure.suppressed, measure.notes, measure.excluded) == (None, 10, 0)


def test_a_record_stored_before_the_second_stamp_starts_the_policy_count(
    redis_fakes: RedisFakes,
) -> None:
    """An old record has no policy_version: it resolves null, the next is -1."""
    section = {UNIT_A_SECTION: {"config_version": FIRST}}
    redis_fakes.operational.store[override_key("tenant-a")] = json.dumps(
        {"version": FIRST, "set_at": "t", "sections": section}
    )
    resolved = run(resolve_section("tenant-a", UNIT_A_SECTION))
    assert (resolved.source, resolved.policy_version) == ("override", None)
    record = run(_set({"enforcement_mode": "off"}))
    assert (record.version, record.policy_version) == (FIRST, f"{POLICY}1")


def test_every_mark_affecting_field_is_a_config_field() -> None:
    """A renamed field must not silently drop out of the one list."""
    assert set(MARK_AFFECTING_FIELDS) <= {
        field.name for field in dataclasses.fields(TenantConfig)
    }


# --- helpers ----------------------------------------------------------------

NOW = datetime.now(UTC)


def run(coro):
    """Drive one coroutine to completion from a sync test."""
    import asyncio

    return asyncio.run(coro)
