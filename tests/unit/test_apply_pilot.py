"""DoDeal's pilot settings (scripts/pilot): both bodies are complete admin PUT
bodies the section parsers accept, with the values the pilot asked for; the
script refuses any PLACEHOLDER before it sends anything, and PUTs both
sections through the admin routes with the token from the environment."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from dodeal_ai.units.call_intelligence.config import CallsConfig, parse_unit_b_section
from dodeal_ai.units.structured_intelligence.config import (
    TenantConfigFile,
    parse_unit_a_section,
)
from dodeal_ai.units.structured_intelligence.schemas import EnforcementMode
from scripts.pilot import apply_pilot

UNIT_A = json.loads((apply_pilot.HERE / "dodeal_unit_a.json").read_text("utf-8"))
UNIT_B = json.loads((apply_pilot.HERE / "dodeal_unit_b.json").read_text("utf-8"))
TOKEN = "invented-service-token"
FILLED_B = {
    **UNIT_B,
    "callback_url": "https://crm.invented.example/hooks/calls",
    "audio_hosts": ["audio.invented.example"],
    "alarm_phrases": ["invented alarm"],
}
FILLED_A = {**UNIT_A, "blocking_stages": ["invented-stage"]}


def test_both_bodies_validate_against_the_schemas() -> None:
    """Every field the section may set, by its schema name; the service
    stamps config_version, so neither body carries one."""
    assert set(UNIT_A) == set(TenantConfigFile.model_fields) - {"config_version"}
    assert set(UNIT_B) == set(CallsConfig.model_fields) - {"config_version"}
    unit_a = parse_unit_a_section({**UNIT_A, "config_version": "v"})
    unit_b = parse_unit_b_section(UNIT_B)

    assert unit_a.pilot and unit_a.blocking_enabled and unit_a.rep_numbers_enabled
    assert unit_a.enforcement_mode is EnforcementMode.STRICT
    assert unit_a.deal_specifics_applicable
    assert unit_b.pilot and unit_b.calls_enabled and unit_b.scoring_enabled
    assert unit_b.number_detection_enabled and unit_b.alarm_phrases_enabled
    assert not unit_b.voice_id_enabled
    assert unit_b.timezone == "Asia/Dubai"
    assert unit_b.keyword_vocabulary == {"Peace Homes Skyline", "بيس هومز سكاي لاين"}


def test_the_placeholders_are_the_ones_the_doc_lists() -> None:
    found = apply_pilot.placeholders(UNIT_A, "unit_a") + apply_pilot.placeholders(
        UNIT_B, "unit_b"
    )
    assert found == [
        "unit_a.blocking_stages[0]",
        "unit_a.blocking_stages[1]",
        "unit_a.blocking_stages[2]",
        "unit_b.callback_url",
        "unit_b.audio_hosts[0]",
        "unit_b.alarm_phrases[0]",
    ]
    doc = (Path(__file__).parents[2] / "docs/pilot/dodeal.md").read_text("utf-8")
    assert all(f"`{path}`" in doc for path in found)


class _Service:
    """The admin routes on a MockTransport: records every request."""

    def __init__(self, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.status = status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            self.status, json={"version": "v1", "policy_version": "p1"}
        )


def _files(tmp_path: Path, unit_a: dict, unit_b: dict) -> list[str]:
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    a.write_text(json.dumps(unit_a), "utf-8")
    b.write_text(json.dumps(unit_b), "utf-8")
    return ["--unit-a", str(a), "--unit-b", str(b)]


def _run(argv: list[str], service: _Service) -> int:
    return apply_pilot.main(
        ["--base-url", "http://service.invented", *argv],
        transport=httpx.MockTransport(service),
    )


def test_apply_pilot_refuses_a_placeholder_and_sends_nothing(
    monkeypatch, capsys
) -> None:
    monkeypatch.setenv(apply_pilot.TOKEN_ENV, TOKEN)
    service = _Service()

    assert _run([], service) == 1
    assert service.requests == []
    out = capsys.readouterr().out
    assert "unit_b.callback_url" in out
    assert "PLACEHOLDER" not in out


def test_one_placeholder_left_in_one_section_refuses_both(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(apply_pilot.TOKEN_ENV, TOKEN)
    service = _Service()
    left = {**FILLED_B, "alarm_phrases": ["invented", "placeholder too"]}

    assert _run(_files(tmp_path, FILLED_A, left), service) == 1
    assert service.requests == []


def test_filled_bodies_are_put_through_the_admin_routes(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(apply_pilot.TOKEN_ENV, TOKEN)
    service = _Service()

    assert _run(_files(tmp_path, FILLED_A, FILLED_B), service) == 0
    a, b = service.requests
    assert [(r.method, r.url.path) for r in (a, b)] == [
        ("PUT", "/api/v1/admin/tenant-config"),
        ("PUT", "/api/v1/admin/tenant-config/unit_b"),
    ]
    assert json.loads(a.content) == FILLED_A
    assert json.loads(b.content) == FILLED_B
    for request in (a, b):
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["host"] == "dodeal.dodealcrm.com"
    assert TOKEN not in capsys.readouterr().out


def test_dry_run_only_validates(tmp_path, capsys) -> None:
    service = _Service()
    argv = ["--dry-run", *_files(tmp_path, FILLED_A, FILLED_B)]

    assert _run(argv, service) == 0
    assert service.requests == []
    assert "nothing sent" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("unit_a", "unit_b"),
    [
        ({**FILLED_A, "enforcement_mode": "blocking"}, FILLED_B),
        (FILLED_A, {**FILLED_B, "callback_url": "http://crm.invented.example/x"}),
        (FILLED_A, {**FILLED_B, "unknown_switch": True}),
    ],
)
def test_a_body_the_schema_refuses_is_named_and_not_sent(
    tmp_path, monkeypatch, capsys, unit_a, unit_b
) -> None:
    monkeypatch.setenv(apply_pilot.TOKEN_ENV, TOKEN)
    service = _Service()

    assert _run(_files(tmp_path, unit_a, unit_b), service) == 1
    assert service.requests == []
    out = capsys.readouterr().out
    assert "refused by the section's schema" in out
    assert "crm.invented.example" not in out


def test_no_token_or_no_base_url_is_refused(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(apply_pilot.TOKEN_ENV, raising=False)
    service = _Service()
    files = _files(tmp_path, FILLED_A, FILLED_B)

    assert _run(files, service) == 1
    monkeypatch.setenv(apply_pilot.TOKEN_ENV, TOKEN)
    assert apply_pilot.main(files, transport=httpx.MockTransport(service)) == 1
    assert service.requests == []


def test_a_refused_put_stops_and_says_what_was_applied(
    tmp_path, monkeypatch, capsys
) -> None:
    monkeypatch.setenv(apply_pilot.TOKEN_ENV, TOKEN)
    service = _Service(status=422)

    assert _run(_files(tmp_path, FILLED_A, FILLED_B), service) == 1
    assert len(service.requests) == 1
    assert "applied before it: nothing" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["not json", "[1, 2]"])
def test_a_file_that_is_not_an_object_is_refused(tmp_path, content) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(content, "utf-8")

    assert apply_pilot.main(["--dry-run", "--unit-a", str(bad)]) == 1
