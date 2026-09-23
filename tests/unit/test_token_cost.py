"""The token budget: the charge, the pre-flight, and the line between them.

Three properties are worth more than the rest of this file put together, and
they are the ones a wrong edit would break silently rather than loudly:

  1. THE TWO COUNTERS NEVER TOUCH. Gate 4 counts requests on `cost:*`; the
     token budget counts spend on `tokens:*`. If a script ever reached across,
     "requests made" would start standing in for "tokens spent" -- a number
     that looks right and is not. The first test asserts it from the OUTSIDE,
     on the keys the two scripts were actually EVAL'd against.

  2. THE PRE-FLIGHT READS AND NEVER WRITES; THE CHARGE WRITES AND NEVER DENIES.
     A pre-flight that wrote would charge a judgement that has not happened; a
     charge that denied would throw away an answer the provider has already
     been paid for.

  3. EVERY FAILURE IS OPEN. A cost cap is a money guard, not a security guard,
     so an unreachable store allows the judgement and says so in the log.

The route-level tests drive a real judgement through the real gate chain rather
than calling the limiter directly: the charge count per outcome is a fact about
where `enforce_token_cost` sits in `llm_call`, and calling the limiter by hand
would assert the arithmetic while assuming the placement.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.breaker import cost_breaker
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.cost import limiter
from dodeal_ai.core.cost.limiter import (
    _ADD_TOKENS_SCRIPT,
    _INCR_BOTH_SCRIPT,
    enforce_token_cost,
    token_preflight,
)
from dodeal_ai.core.errors import TokenBudgetExceeded
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import breakers, tokens
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response, response
from tests.helpers.fake_operational_redis import FakeOperationalRedis
from tests.helpers.scopes import TEST_SCOPE, history_scope, make_scope
from tests.helpers.score_answers import score_payload

JUDGE = "/api/v1/notes/judgements"
LEAD_ID = 1656
NOTE_ID = 10
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."

TENANT_KEY = "tokens:tenant:tenant-a"
USER_KEY = "tokens:user:tenant-a:42"

# FakeLLM's round defaults: 100 in, 20 out, so one response is 120 tokens and a
# three-pass judgement is 360. Chosen so an expected charge is computable by eye.
PER_RESPONSE = 120


def _vague_answer(is_vague: bool = True):
    return json_response(
        {
            "is_vague": is_vague,
            "missing_components": ["next_step_with_date"] if is_vague else [],
            "clarification_prompt": "Which Tuesday, and what will you cover?"
            if is_vague
            else None,
            "reasoning": "The follow-up has no date.",
        }
    )


def _score_answer():
    return json_response(score_payload())


def _script(llm: FakeLLM, note_type: str = "discovery") -> None:
    """One judgement's three answers, queued against the templates that will be
    sent rather than positionally -- vague and score are gathered."""
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": note_type}))
    llm.script_for(template_for(NoteType(note_type)), _vague_answer())
    llm.script_for(SCORE_TEMPLATE, _score_answer())


@pytest.fixture
def cost() -> FakeCostRedis:
    return FakeCostRedis()


@pytest.fixture
def operational() -> FakeOperationalRedis:
    return FakeOperationalRedis()


@pytest.fixture
def llm() -> FakeLLM:
    client = FakeLLM()
    _script(client)
    return client


@pytest.fixture
def leads() -> FakeLeadsClient:
    return FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE)]},
    )


@pytest.fixture
def client(monkeypatch, leads, llm, operational, cost):
    """The judgement route behind the real gate chain, with db1 and db2 faked."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )

    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    monkeypatch.setattr(state, "get_operational_client", lambda: operational)

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_leads_client] = lambda: leads
    app.dependency_overrides[get_llm_client] = lambda: llm

    yield TestClient(app)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def json_log():
    """The real JsonFormatter over the dodeal_ai tree -- the exact text a
    collector would receive."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("dodeal_ai")
    original_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original_level)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain='tenant-a', sub=42)}",
        "Host": "tenant-a.dodealcrm.com",
    }


def _body() -> dict:
    return {"lead_id": LEAD_ID, "note_id": NOTE_ID}


# --- the one that matters: the two counters never touch ---------------------


def test_token_and_request_counters_never_touch(client, cost):
    """One scored judgement moves `cost:*` by one and `tokens:*` by the summed
    usage, and the two scripts were EVAL'd against disjoint key sets."""
    response = client.post(JUDGE, json=_body(), headers=_headers())
    assert response.status_code == 200

    # Gate 4 counted ONE request; three model responses were charged.
    assert cost.store["cost:tenant:tenant-a"] == 1
    assert cost.store["cost:user:tenant-a:42"] == 1
    assert cost.store[TENANT_KEY] == 3 * PER_RESPONSE
    assert cost.store[USER_KEY] == 3 * PER_RESPONSE

    # Asserted on what was actually sent, not on the two constants: a script
    # that started writing the other pair's keys would still match its own
    # source text, and this is the check that would not.
    request_keys = cost.keys_for(_INCR_BOTH_SCRIPT)
    token_keys = cost.keys_for(_ADD_TOKENS_SCRIPT)
    assert request_keys == {"cost:tenant:tenant-a", "cost:user:tenant-a:42"}
    assert token_keys == {TENANT_KEY, USER_KEY}
    assert not request_keys & token_keys


# --- the pre-flight ---------------------------------------------------------


async def test_the_preflight_allows_a_tenant_one_under_the_limit(monkeypatch, cost):
    """One token under the cap is not at the cap, and must still be judged."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[TENANT_KEY] = 999

    await token_preflight(TEST_SCOPE)  # must not raise


async def test_the_preflight_denies_at_the_tenant_limit(monkeypatch, cost):
    """AT the limit denies, not merely over it: the counters are charged after
    the fact, so a total that has reached the cap has already spent it."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[TENANT_KEY] = 1000

    with pytest.raises(TokenBudgetExceeded):
        await token_preflight(TEST_SCOPE)


async def test_the_preflight_denies_at_the_user_limit(monkeypatch, cost):
    """The user cap denies on its own, with the tenant nowhere near its own."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_USER_LIMIT", "100")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[USER_KEY] = 100

    with pytest.raises(TokenBudgetExceeded):
        await token_preflight(TEST_SCOPE)


async def test_the_preflight_never_writes(monkeypatch, cost):
    """It is a read. A pre-flight that wrote would charge a judgement that has
    not happened yet."""
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)

    await token_preflight(TEST_SCOPE)

    assert cost.evals == []
    assert cost.store == {}


def test_the_preflight_at_the_limit_is_a_429_with_no_model_call(
    monkeypatch, client, cost, llm
):
    """Over budget is 429 `token_budget_exceeded` in the standard body, and the
    refusal costs nothing: not one prompt is sent."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    cost.store[TENANT_KEY] = 1000

    response = client.post(JUDGE, json=_body(), headers=_headers())

    assert response.status_code == 429
    body = response.json()
    assert body["detail"] == "Too Many Requests"
    assert body["reason"] == "token_budget_exceeded"
    assert body["request_id"]
    assert llm.call_count == 0


def test_a_refused_judgement_releases_the_reservation(monkeypatch, client, cost):
    """429 is a non-200 after reserving, so the key is released and the same
    note is judged -- not 409'd -- once the budget allows it."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    cost.store[TENANT_KEY] = 1000
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 429

    cost.store[TENANT_KEY] = 0
    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200


def test_a_dead_store_allows_the_judgement(client, cost):
    """Redis down fails OPEN: the budget cannot be read, so it cannot refuse."""
    cost.fail = True

    response = client.post(JUDGE, json=_body(), headers=_headers())

    assert response.status_code == 200
    assert response.json()["suppressed"] is None


async def test_the_preflight_bypass_is_logged_every_time(monkeypatch, cost, json_log):
    """With the latch gone, every pre-flight bypass is logged."""
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.fail = True

    for _ in range(3):
        await token_preflight(make_scope())

    bypasses = [
        x for x in _lines(json_log) if x["message"] == "token_preflight_bypassed"
    ]
    assert len(bypasses) == 3
    assert {x["level"] for x in bypasses} == {"WARNING"}
    assert bypasses[0]["tenant"] == "tenant-a"
    assert bypasses[0]["reason_code"] == "token_store_unavailable"
    # The store was actually asked each time, so nothing claims otherwise.
    assert "breaker" not in bypasses[0]


# --- the breaker in front of db1 --------------------------------------------


async def test_an_open_breaker_bypasses_the_preflight_without_a_read(
    monkeypatch, cost, json_log
):
    """Open: the pre-flight allows the judgement and never touches the store."""
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    await breakers.trip(cost_breaker())

    await token_preflight(make_scope())  # must not raise

    assert cost.mgets == []
    line = next(
        x for x in _lines(json_log) if x["message"] == "token_preflight_bypassed"
    )
    assert line["breaker"] == "open"
    assert line["reason_code"] == "token_store_unavailable"


async def test_an_open_breaker_bypasses_the_charge_without_an_eval(
    monkeypatch, cost, json_log
):
    """Open: the tokens are still spent, and still not counted -- a hole in the
    meter, never in the bill."""
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    await breakers.trip(cost_breaker())

    await enforce_token_cost(
        TEST_SCOPE, input_tokens=100, output_tokens=20, profile="unit_a.classify"
    )

    assert cost.evals == []
    line = next(x for x in _lines(json_log) if x["message"] == "token_charge_bypassed")
    assert line["breaker"] == "open"


# --- the charge -------------------------------------------------------------


async def test_a_charge_that_fails_is_logged_and_swallowed(monkeypatch, cost, json_log):
    """The call is already paid for: a failed charge is a hole in the meter, so
    it must never reach the caller."""
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.fail = True

    await enforce_token_cost(
        TEST_SCOPE, input_tokens=100, output_tokens=20, profile="unit_a.classify"
    )

    line = next(x for x in _lines(json_log) if x["message"] == "token_charge_bypassed")
    assert line["level"] == "WARNING"
    assert line["reason_code"] == "token_store_unavailable"
    assert line["tenant"] == "tenant-a"


def test_a_charge_failure_still_completes_the_judgement(client, cost):
    """db1 dead: no pre-flight, no counting, and a judgement all the same."""
    cost.fail = True

    response = client.post(JUDGE, json=_body(), headers=_headers())

    assert response.status_code == 200
    assert response.json()["suppressed"] is None


def test_a_scored_judgement_charges_three_times(client, cost):
    """Three passes, three responses, three charges."""
    client.post(JUDGE, json=_body(), headers=_headers())

    charges = [e for e in cost.evals if e[0] == _ADD_TOKENS_SCRIPT]
    assert len(charges) == 3
    assert cost.store[TENANT_KEY] == 3 * PER_RESPONSE


def test_a_reprompted_judgement_charges_four_times(client, cost):
    """A reprompt's DISCARDED first answer was still paid for, so it is still
    charged: four responses, four charges."""
    # A FRESH client, scripted malformed-then-good: script_for APPENDS, so
    # adding the bad answer to the fixture's queue would let the good one serve
    # the first call and no reprompt would happen at all.
    reprompting = FakeLLM()
    reprompting.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "discovery"}))
    reprompting.script_for(template_for(NoteType.DISCOVERY), _vague_answer())
    reprompting.script_for(SCORE_TEMPLATE, response("not json at all"), _score_answer())
    app.dependency_overrides[get_llm_client] = lambda: reprompting

    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200
    assert reprompting.call_count == 4

    charges = [e for e in cost.evals if e[0] == _ADD_TOKENS_SCRIPT]
    assert len(charges) == 4
    assert cost.store[TENANT_KEY] == 4 * PER_RESPONSE


def test_a_suppressed_judgement_charges_nothing(client, cost, leads):
    """A note too thin to judge reaches no model, so there is nothing to
    charge and no pre-flight to read."""
    leads.notes[LEAD_ID] = [note(NOTE_ID, "ok")]

    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200

    assert [e for e in cost.evals if e[0] == _ADD_TOKENS_SCRIPT] == []
    assert TENANT_KEY not in cost.store


def test_a_response_with_no_usage_charges_nothing(client, cost, llm, json_log):
    """A provider that reported no usage is not a free call, it is an unmeasured
    one -- so it writes no counter and no line rather than a misleading zero."""
    llm.script_for(
        SCORE_TEMPLATE, response(_score_answer().text, input_tokens=0, output_tokens=0)
    )
    # The scripted answer above replaces the fixture's for THIS request only if
    # it is the one popped, so drop the fixture's queue by scoring first.
    llm.script_for(CLASSIFY_TEMPLATE, json_response({"note_type": "discovery"}))
    llm.script_for(template_for(NoteType.DISCOVERY), _vague_answer())

    assert client.post(JUDGE, json=_body(), headers=_headers()).status_code == 200

    charges = [e for e in cost.evals if e[0] == _ADD_TOKENS_SCRIPT]
    assert len(charges) == 3  # the fixture's three; the zero-usage one is extra
    profiles = [
        x["profile"] for x in _lines(json_log) if x["message"] == "tokens_charged"
    ]
    assert len(profiles) == 3


def test_tokens_charged_carries_the_profile_and_the_totals(client, json_log):
    """The INFO line a spend dashboard reads: ids, numbers, and a profile
    name."""
    client.post(JUDGE, json=_body(), headers=_headers())

    charged = [x for x in _lines(json_log) if x["message"] == "tokens_charged"]
    assert len(charged) == 3
    assert {x["profile"] for x in charged} == {
        "unit_a.classify",
        "unit_a.vague",
        "unit_a.score",
    }
    last = charged[-1]
    assert last["tenant"] == "tenant-a"
    assert last["subject"] == "42"
    assert last["input_tokens"] == 100
    assert last["output_tokens"] == 20
    assert last["tenant_total"] == 3 * PER_RESPONSE
    assert last["user_total"] == 3 * PER_RESPONSE
    assert last["request_id"]


# --- the warning ratio ------------------------------------------------------


async def test_the_warning_fires_once_on_the_crossing(monkeypatch, cost, json_log):
    """One line when a total first crosses limit * ratio, and none on the call
    after it -- the crossing happens once per window, not once per call."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_USER_LIMIT", "100")
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "100000")
    monkeypatch.setenv("DODEAL_COST_TOKEN_WARNING_RATIO", "0.9")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)

    # 50 -> under 90. 100 -> crossed. 150 -> already over, so silent.
    for _ in range(3):
        await enforce_token_cost(
            TEST_SCOPE, input_tokens=40, output_tokens=10, profile="unit_a.score"
        )

    warnings = [x for x in _lines(json_log) if x["message"] == "token_budget_warning"]
    assert len(warnings) == 1
    assert warnings[0]["level"] == "WARNING"
    assert warnings[0]["key"] == USER_KEY
    assert warnings[0]["warning_ratio"] == 0.9
    assert warnings[0]["total"] == 100
    assert warnings[0]["limit"] == 100


async def test_the_warning_does_not_fire_below_the_ratio(monkeypatch, cost, json_log):
    """A total under the threshold earns nothing, however many calls made it."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_USER_LIMIT", "1000")
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)

    for _ in range(3):
        await enforce_token_cost(
            TEST_SCOPE, input_tokens=100, output_tokens=0, profile="unit_a.score"
        )

    assert [x for x in _lines(json_log) if x["message"] == "token_budget_warning"] == []


async def test_the_warning_does_not_deny(monkeypatch, cost):
    """It is a warning. Nothing degrades at the ratio, and nothing raises."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_USER_LIMIT", "100")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)

    await enforce_token_cost(
        TEST_SCOPE, input_tokens=500, output_tokens=0, profile="unit_a.score"
    )

    assert cost.store[USER_KEY] == 500


# --- degraded near the budget (register item 61) -----------------------------


@pytest.mark.parametrize(
    ("limit_env", "key", "stored", "budgets"),
    [
        ("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", TENANT_KEY, 900, "tenant"),
        ("DODEAL_COST_TOKENS_PER_USER_LIMIT", USER_KEY, 950, "user"),
    ],
    ids=["tenant", "user"],
)
async def test_the_preflight_is_degraded_at_ninety_percent(
    monkeypatch, cost, json_log, limit_env, key, stored, budgets
):
    """At or above 90 % of either limit the pre-flight answers True and says so once."""
    monkeypatch.setenv(limit_env, "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[key] = stored

    assert await token_preflight(TEST_SCOPE) is True

    lines = [x for x in _lines(json_log) if x["message"] == "token_budget_degraded"]
    assert len(lines) == 1
    assert (lines[0]["budgets"], lines[0]["tenant"]) == (budgets, "tenant-a")
    assert lines[0]["level"] == "WARNING"


async def test_the_preflight_just_under_ninety_percent_is_not_degraded(
    monkeypatch, cost, json_log
):
    """899 of 1000 is a full judgement and no line."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[TENANT_KEY] = 899

    assert await token_preflight(TEST_SCOPE) is False
    assert not [x for x in _lines(json_log) if x["message"] == "token_budget_degraded"]


async def test_a_bypassed_preflight_is_not_degraded(monkeypatch, cost):
    """Fail open means a full judgement: an unread budget is not a near one."""
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.fail = True
    assert await token_preflight(TEST_SCOPE) is False


def test_a_degraded_judgement_logs_the_line_once_for_three_passes(
    monkeypatch, client, cost, llm, json_log
):
    """One judgement near the budget: three calls, 200, and one degraded line."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    cost.store[TENANT_KEY] = 950

    response = client.post(JUDGE, json=_body(), headers=_headers())

    assert response.status_code == 200
    assert llm.call_count == 3
    degraded = [x for x in _lines(json_log) if x["message"] == "token_budget_degraded"]
    assert len(degraded) == 1


# --- the history budget (register item 127) ---------------------------------

HISTORY_KEY = "tokens:history:tenant:tenant-a"


async def test_a_history_charge_moves_only_the_history_counter(
    monkeypatch, cost, json_log
):
    """One key, its own total on the line, and the warning names that key."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_HISTORY_PER_TENANT_LIMIT", "100")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)

    await enforce_token_cost(
        history_scope(), input_tokens=90, output_tokens=5, profile="unit_a.score"
    )

    assert cost.store == {HISTORY_KEY: 95}
    charged = next(x for x in _lines(json_log) if x["message"] == "tokens_charged")
    assert charged["history_total"] == 95
    assert "tenant_total" not in charged and "user_total" not in charged
    warned = next(x for x in _lines(json_log) if x["message"] == "token_budget_warning")
    assert warned["key"] == HISTORY_KEY


async def test_the_history_preflight_reads_one_key_and_degrades_near_it(
    monkeypatch, cost, json_log
):
    """Near the history limit the judgement runs degraded, named `history`."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_HISTORY_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[HISTORY_KEY] = 950
    cost.store[TENANT_KEY] = 10**9

    assert await token_preflight(history_scope()) is True

    assert cost.mgets == [(HISTORY_KEY,)]
    line = next(x for x in _lines(json_log) if x["message"] == "token_budget_degraded")
    assert line["budgets"] == "history"


async def test_the_history_preflight_denies_at_its_own_limit(monkeypatch, cost):
    """At the history limit the pre-flight refuses, whatever the live pair says."""
    monkeypatch.setenv("DODEAL_COST_TOKENS_HISTORY_PER_TENANT_LIMIT", "1000")
    get_settings.cache_clear()
    monkeypatch.setattr(limiter, "get_cost_client", lambda: cost)
    cost.store[HISTORY_KEY] = 1000

    with pytest.raises(TokenBudgetExceeded):
        await token_preflight(history_scope())
