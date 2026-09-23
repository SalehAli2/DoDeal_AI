"""The direct judgement routes (DECISION[DIRECT_ROUTE]): the same judgement, on
text the CRM sent instead of text we fetched.

WHAT THESE TESTS ARE FOR. The direct route is the one exception to "note text is
never accepted in a request body", and the whole case for admitting it is that
it changes NOTHING except where the text came from. That claim is only worth
anything if something checks it, so the two tests at the top of this file put
the same text through both routes and compare: the same judgement, field for
field but the request id, and the same fingerprint -- which is what makes a note
judged on one route a duplicate on the other.

Everything after those two is the direct route's own behaviour at the points the
fetch route has a stop: the length gate at both ends, the reservation, the
resubmission variant, the thing the route must NOT do -- fetch anything -- and,
since register item 92, the credential: the CRM's service token only, with the
prompt caps keyed on the author the body names.
"""

from __future__ import annotations

import io
import json
import logging

import pytest
from fastapi.testclient import TestClient

import dodeal_ai.core.cost.limiter as cost_limiter
from dodeal_ai.api.routes.judgements import router as judgements_router
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.logging_config import JsonFormatter
from dodeal_ai.main import app
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.fake_operational_redis import FakeOperationalRedis
from tests.helpers.score_answers import score_payload

JUDGE = "/api/v1/notes/judgements"
DIRECT = "/api/v1/notes/judgements/direct"
DIRECT_RESUBMIT = "/api/v1/notes/judgements/direct/resubmission"

LEAD_ID = 1656
NOTE_ID = 10
AUTHOR_ID = 27
SUBJECT = 42
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."

CONFIG = get_tenant_config("tenant-a")

# The four lead fields the classifier's context section reads, and the only
# ones the direct body may carry.
LEAD_CONTEXT = {
    "leadType": "buyer",
    "enquiryType": "sale",
    "project": "New Cairo",
    "status": "warm",
}

CLASSIFY_AS = NoteType.DISCOVERY
VAGUE_ANSWER = {
    "is_vague": True,
    "missing_components": ["next_step_with_date"],
    "clarification_prompt": "Which Tuesday are you calling, and what will you cover?",
    "reasoning": "The follow-up has no date.",
}
SCORE_ANSWER = score_payload()


def _script(llm: FakeLLM, judgements: int = 1) -> None:
    """One judgement's three answers, queued BY TEMPLATE rather than by arrival
    order.

    `script_for`, not the positional script: vague detection and scoring are
    issued together under asyncio.gather, and arrival order between them is a
    scheduling accident. A test that compares two routes must not be asserting
    that accident by accident.
    """
    llm.script_for(
        CLASSIFY_TEMPLATE,
        *[json_response({"note_type": CLASSIFY_AS.value})] * judgements,
    )
    llm.script_for(
        template_for(CLASSIFY_AS), *[json_response(VAGUE_ANSWER)] * judgements
    )
    llm.script_for(SCORE_TEMPLATE, *[json_response(SCORE_ANSWER)] * judgements)


class _ExplodingLeadsClient:
    """A leads client that raises on ANY call.

    Injected at `get_leads_client` for every test in this file. The direct route
    does not depend on it, so it is never even resolved -- but if the route ever
    grows a fetch, or the pipeline reaches `_fetch_note` on this path, the test
    that would otherwise quietly pass fails by name instead.
    """

    async def get_leads(self, *args, **kwargs):
        raise AssertionError("the direct route fetched get_leads")

    async def get_lead(self, *args, **kwargs):
        raise AssertionError("the direct route fetched get_lead")

    async def get_lead_notes(self, *args, **kwargs):
        raise AssertionError("the direct route fetched get_lead_notes")


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def operational() -> FakeOperationalRedis:
    return FakeOperationalRedis()


@pytest.fixture
def cost() -> FakeCostRedis:
    """db1, shared by both clients in this file: the pre-flight reads it and
    the charge writes it on every judgement either route produces."""
    return FakeCostRedis()


@pytest.fixture
def leads() -> _ExplodingLeadsClient:
    return _ExplodingLeadsClient()


@pytest.fixture
def client(monkeypatch, leads, llm, operational, cost):
    """The gate chain plus the three seams, with a leads client that raises."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )

    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: cost)
    monkeypatch.setattr(state, "get_operational_client", lambda: operational)

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_leads_client] = lambda: leads
    app.dependency_overrides[get_llm_client] = lambda: llm

    yield TestClient(app)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def fetch_client(monkeypatch, llm, operational, cost):
    """The SAME wiring with a real fake CRM behind it, for the comparison tests.

    A second fixture rather than a flag on the first: the two comparison tests
    need a client that CAN fetch, and every other test in this file needs one
    that cannot, and a single fixture doing both would be one `if` away from
    letting a direct-route test fetch by accident.
    """
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    tokens.service_settings_env(monkeypatch)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )
    fake_crm = FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID, **LEAD_CONTEXT)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE, author_id=AUTHOR_ID)]},
    )

    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: cost)
    monkeypatch.setattr(state, "get_operational_client", lambda: operational)

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_leads_client] = lambda: fake_crm
    app.dependency_overrides[get_llm_client] = lambda: llm

    yield TestClient(app)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.fixture
def json_log():
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


def _user_headers(subdomain: str = "tenant-a", sub: int = SUBJECT) -> dict:
    """A person's token: the fetch routes' credential."""
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain=subdomain, sub=sub)}",
        "Host": f"{subdomain}.dodealcrm.com",
    }


def _headers(subdomain: str = "tenant-a") -> dict:
    """The CRM's service token: the direct routes' only credential (item 92)."""
    return {
        "Authorization": f"Bearer {tokens.mint_service_token(subdomain=subdomain)}",
        "Host": f"{subdomain}.dodealcrm.com",
    }


def _direct_body(text: str = GOOD_NOTE, **overrides) -> dict:
    body = {
        "lead_id": LEAD_ID,
        "note_id": NOTE_ID,
        "author_id": AUTHOR_ID,
        "note_text": text,
        "lead": dict(LEAD_CONTEXT),
    }
    body.update(overrides)
    return body


def _fetch_body() -> dict:
    return {"lead_id": LEAD_ID, "note_id": NOTE_ID}


def _note_keys(operational: FakeOperationalRedis) -> set[str]:
    """Every db2 key that belongs to the NOTE, not to whoever is asking."""
    return {key for key in operational.store if not key.startswith("ratelimit")}


def _without_request_id(judgement: dict) -> dict:
    return {k: v for k, v in judgement.items() if k != "request_id"}


def _dependency_calls(path: str) -> set:
    """Every callable in one route's WHOLE dependency tree.

    The whole tree, not the route signature's own Depends: the gates chain
    through nested Depends, so a client resolved two levels down would not show
    up in a shallow read -- and "not a dependency of this route" has to mean the
    whole tree or it means nothing.

    Read off the ROUTER rather than `app.routes`, which nests included routers.
    """
    route = next(r for r in judgements_router.routes if r.path == path)
    calls: set = set()
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        calls.add(dependant.call)
        pending.extend(dependant.dependencies)
    return calls


# --- the claim the decision rests on: the same judgement -------------------


def test_the_direct_route_produces_the_same_judgement_as_the_fetch_route(
    fetch_client, llm, operational
):
    """The same text, the same answers, the same judgement.

    Both requests run through the same FakeLLM with the same template-scripted
    answers, so the only difference between them is how the note reached the
    pipeline. Everything but the request id must match -- including the score,
    the decision, the attempt counts and all four version stamps.
    """
    _script(llm, judgements=2)

    fetched = fetch_client.post(JUDGE, json=_fetch_body(), headers=_user_headers())
    assert fetched.status_code == 200

    # The same note text is a duplicate of itself, so the reservation from the
    # first request has to go before the second can be judged at all. That is
    # the fingerprint agreeing across the two routes, asserted directly in the
    # next test; here it is cleared so the comparison is possible.
    operational.store.clear()

    direct = fetch_client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert direct.status_code == 200

    assert _without_request_id(direct.json()) == _without_request_id(fetched.json())
    # And the one field that legitimately differs, differs.
    assert direct.json()["request_id"] != fetched.json()["request_id"]


def test_the_same_text_gives_the_same_fingerprint_on_both_routes(
    fetch_client, llm, operational
):
    """The idempotency key is the CONTENT, so it cannot depend on the route.

    If it did, a note judged on the fetch route could be judged and paid for a
    second time on the direct route -- which is exactly the double-spend the
    reservation exists to stop.
    """
    _script(llm, judgements=2)

    fetch_client.post(JUDGE, json=_fetch_body(), headers=_user_headers())
    fetched_keys = _note_keys(operational)

    operational.store.clear()
    fetch_client.post(DIRECT, json=_direct_body(), headers=_headers())
    direct_keys = _note_keys(operational)

    # The note's keys match across routes. The rate-limit keys legitimately do
    # not since register item 92: they name the token's sub on one route and
    # author:<id> on the other.
    assert fetched_keys == direct_keys
    assert any(key.startswith("idem:") for key in direct_keys)
    expected = state.note_fingerprint(GOOD_NOTE)
    assert any(expected in key for key in direct_keys)
    # The text itself is nowhere near the key.
    assert not any(GOOD_NOTE in key for key in direct_keys)


def test_a_note_judged_on_the_fetch_route_is_a_duplicate_on_the_direct_route(
    fetch_client, llm
):
    # The consequence of the two above, stated as behaviour: one note, one paid
    # judgement, whichever door it came through.
    _script(llm, judgements=1)

    assert (
        fetch_client.post(
            JUDGE, json=_fetch_body(), headers=_user_headers()
        ).status_code
        == 200
    )
    second = fetch_client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert llm.call_count == 3


# --- the direct route's own behaviour ---------------------------------------


def test_a_direct_request_is_judged_without_any_fetch(client, llm, leads):
    _script(llm)
    r = client.post(DIRECT, json=_direct_body(), headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert body["suppressed"] is None
    assert body["score"]["band"] == "fair"
    assert body["note_id"] == NOTE_ID
    assert body["lead_id"] == LEAD_ID
    # The author is the CRM's stored author from the body, not the token's sub.
    assert body["author_id"] == AUTHOR_ID
    # _ExplodingLeadsClient raises on any call, so reaching here is the proof.
    assert llm.call_count == 3


def test_a_direct_request_never_calls_the_leads_client(client, llm):
    """The route does not depend on `get_leads_client` and the pipeline never
    reaches `_fetch_note` on this path.

    The injected client raises on every method, so a fetch anywhere in the
    request would surface as a 500 rather than as this 200.
    """
    _script(llm)
    r = client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert r.status_code == 200

    # Belt and braces, and the stronger statement: `get_leads_client` is not
    # anywhere in either route's dependency tree, so it is never resolved and no
    # backend key is looked up for a request that has nothing to call.
    for path in (DIRECT, DIRECT_RESUBMIT):
        assert get_leads_client not in _dependency_calls(path), path


def test_a_second_identical_direct_request_is_a_replay(client, llm):
    _script(llm, judgements=2)

    first = client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert first.status_code == 200

    second = client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"
    assert second.json() == {
        **first.json(),
        "request_id": second.headers["X-Request-ID"],
    }
    assert llm.call_count == 3


def test_the_direct_resubmission_route_replays_too(client, llm):
    _script(llm, judgements=2)
    path = DIRECT + "/resubmission"
    client.post(path, json=_direct_body(), headers=_headers())
    second = client.post(path, json=_direct_body(), headers=_headers())
    assert second.status_code == 200
    assert second.headers["Idempotent-Replay"] == "true"


def test_an_edited_text_is_a_new_judgement_not_a_duplicate(client, llm):
    # The fingerprint is the content, so the edited note is a different note --
    # which is the case the clarification loop depends on.
    _script(llm, judgements=2)

    assert (
        client.post(DIRECT, json=_direct_body(), headers=_headers()).status_code == 200
    )
    edited = client.post(
        DIRECT,
        json=_direct_body(GOOD_NOTE + " Confirmed Tuesday 3pm."),
        headers=_headers(),
    )
    assert edited.status_code == 200


def test_a_thin_note_is_suppressed_without_reserving_anything(client, llm, operational):
    r = client.post(DIRECT, json=_direct_body("ok"), headers=_headers())

    assert r.status_code == 200
    assert r.json()["suppressed"] == {
        "reason": "insufficient_evidence",
        "detail_code": "note_too_short",
        # Register item 64: the fixed question, capped like a model one --
        # both routes behave the same.
        "clarification_prompt": (
            "What happened, what did the client say, and what is the next "
            "step with a date?"
        ),
        "prompt_withheld": None,
    }
    assert r.json()["score"] is None and r.json()["decision"] is None
    # No JUDGEMENT is reserved; the fixed question's own attempt/rate slots
    # are a separate store, and do move.
    assert not any(key.startswith("idem:") for key in operational.store)
    assert llm.call_count == 0  # nothing spent


def test_a_note_over_the_soft_limit_is_suppressed_without_reserving(
    client, llm, operational
):
    """Over max_note_chars: a 200 with a reason, not a 422.

    The note IS saved in the CRM. Refusing to score it is an answer about the
    note -- it is not one interaction -- and the salesperson can split it and
    resubmit at once, because nothing was reserved.
    """
    too_long = "x " * 1200  # 2,400 characters after strip, well over 2,000
    assert len(too_long.strip()) > CONFIG.max_note_chars

    r = client.post(DIRECT, json=_direct_body(too_long), headers=_headers())

    assert r.status_code == 200
    assert r.json()["suppressed"] == {
        "reason": "not_scorable",
        "detail_code": "note_too_long",
        "clarification_prompt": None,
        "prompt_withheld": None,
    }
    assert r.json()["score"] is None and r.json()["decision"] is None
    # No model ran, so nothing is stamped as having run one.
    assert r.json()["versions"]["model_version"] == ""
    assert operational.store == {}
    assert llm.call_count == 0


def test_a_note_at_the_soft_limit_is_judged_normally(client, llm):
    # The boundary is `>`, not `>=`: exactly max_note_chars is still one note.
    _script(llm)
    at_limit = "word " * 399 + "w" * 5  # 2,000 characters after strip
    assert len(at_limit.strip()) == CONFIG.max_note_chars

    r = client.post(DIRECT, json=_direct_body(at_limit), headers=_headers())
    assert r.status_code == 200
    assert r.json()["suppressed"] is None


def test_a_note_over_the_hard_ceiling_is_422_invalid_request(client, llm, operational):
    """Over 4,000 characters: refused by the schema, before the pipeline.

    A different failure from the soft limit and deliberately so -- this one is
    about the REQUEST being too big to accept, so it never becomes a judgement,
    is never fingerprinted and never reaches a log line.
    """
    r = client.post(DIRECT, json=_direct_body("x" * 4001), headers=_headers())

    assert r.status_code == 422
    assert r.json() == {
        "detail": "Unprocessable Entity",
        "reason": "invalid_request",
        "request_id": r.headers["X-Request-ID"],
    }
    assert operational.store == {}
    assert llm.call_count == 0


@pytest.mark.parametrize("value", [0, -1])
@pytest.mark.parametrize("field", ["lead_id", "note_id", "author_id"])
@pytest.mark.parametrize("path", [DIRECT, DIRECT_RESUBMIT])
def test_an_id_below_one_on_the_direct_routes_is_422_without_echo(
    client, llm, operational, path, field, value
):
    """An id of 0 or -1 is a 422 whose body carries neither the field name nor the value, and nothing is reserved or called."""
    r = client.post(path, json=_direct_body(**{field: value}), headers=_headers())

    assert r.status_code == 422
    request_id = r.headers["X-Request-ID"]
    assert r.json() == {
        "detail": "Unprocessable Entity",
        "reason": "invalid_request",
        "request_id": request_id,
    }
    assert field not in r.text
    assert str(value) not in r.text.replace(request_id, "")
    assert operational.store == {}
    assert llm.call_count == 0


def test_an_extra_field_is_422_invalid_request(client):
    r = client.post(
        DIRECT,
        json=_direct_body(note="a second copy of the text"),
        headers=_headers(),
    )
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_request"


def test_an_extra_lead_field_is_422_invalid_request(client):
    # The CRM cannot widen what reaches the classifier's context by adding a
    # field to `lead`.
    body = _direct_body()
    body["lead"] = {**LEAD_CONTEXT, "name": "Jane Doe"}
    r = client.post(DIRECT, json=body, headers=_headers())
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid_request"


# --- the resubmission variant -----------------------------------------------


def test_the_direct_resubmission_withholds_the_prompt(client, llm):
    _script(llm)
    r = client.post(DIRECT_RESUBMIT, json=_direct_body(), headers=_headers())

    assert r.status_code == 200
    decision = r.json()["decision"]
    assert decision["prompt_sent"] is False
    assert decision["prompt_withheld"] == "resubmission"
    assert decision["action"] == "accept_flag_prompt"


def test_the_direct_resubmission_reads_the_original_fingerprint(client, llm):
    """Register item 33, on this route too.

    The first direct request asks a question and writes down the fingerprint of
    the note it asked about; the resubmission of the edited text reads it back,
    so the CRM can link the two without this service holding any history.
    """
    _script(llm, judgements=2)

    first = client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert first.json()["decision"]["prompt_sent"] is True

    again = client.post(
        DIRECT_RESUBMIT,
        json=_direct_body(GOOD_NOTE + " Confirmed Tuesday 3pm."),
        headers=_headers(),
    )
    assert again.status_code == 200
    decision = again.json()["decision"]
    assert decision["original_note_fingerprint"] == state.note_fingerprint(GOOD_NOTE)
    assert decision["attempt"] == 1  # read, never incremented


# --- the author the CRM names (register item 92) ----------------------------


AUTHOR_A = 27
AUTHOR_B = 28


def test_the_outcome_line_carries_no_author_comparison(client, llm, json_log):
    """No token sub reaches this route, so nothing is compared or logged."""
    _script(llm)
    r = client.post(DIRECT, json=_direct_body(), headers=_headers())

    assert r.status_code == 200
    assert r.json()["author_id"] == AUTHOR_ID
    line = next(x for x in _lines(json_log) if x["message"] == "judgement_completed")
    assert "author_differs_from_subject" not in line
    assert GOOD_NOTE not in json.dumps(line)


def test_one_authors_fourth_question_in_an_hour_is_withheld_not_anothers(client, llm):
    """Under ONE service token the caps key on each author: A's fourth vague
    note in the hour is rate_limited while B is still asked."""
    _script(llm, judgements=5)
    headers = _headers()

    def judge(author_id: int, note_id: int) -> dict:
        body = _direct_body(author_id=author_id, note_id=note_id)
        response = client.post(DIRECT, json=body, headers=headers)
        assert response.status_code == 200
        return response.json()["decision"]

    for note_id in (101, 102, 103):
        assert judge(AUTHOR_A, note_id)["prompt_sent"] is True
    fourth = judge(AUTHOR_A, 104)
    assert (fourth["prompt_sent"], fourth["prompt_withheld"]) == (False, "rate_limited")
    assert judge(AUTHOR_B, 105)["prompt_sent"] is True


def test_the_attempt_key_names_the_note_and_not_the_author(client, llm, operational):
    """Attempt keys stay tenant and note id only; the rate keys name the author."""
    _script(llm)
    client.post(DIRECT, json=_direct_body(author_id=AUTHOR_A), headers=_headers())

    assert f"attempt:tenant-a:{NOTE_ID}" in operational.store
    assert f"ratelimit:tenant-a:author:{AUTHOR_A}" in operational.store
    assert not any(key.startswith("ratelimit:tenant-a:42") for key in operational.store)


# --- the gates, on the new routes -------------------------------------------


def test_both_direct_routes_are_gated(client):
    assert client.post(DIRECT, json=_direct_body()).status_code == 401
    assert client.post(DIRECT_RESUBMIT, json=_direct_body()).status_code == 401


def test_a_live_direct_judgement_counts_on_the_tenant_request_key_alone(
    client, llm, cost
):
    """Register item 153: live judgements keep cost:tenant, and only it."""
    _script(llm)
    assert (
        client.post(DIRECT, json=_direct_body(), headers=_headers()).status_code == 200
    )
    assert {key for key in cost.store if key.startswith("cost:")} == {
        "cost:tenant:tenant-a"
    }


@pytest.mark.parametrize("path", [DIRECT, DIRECT_RESUBMIT])
def test_a_user_token_is_401_on_both_direct_routes(client, llm, path):
    """Register item 92: a person's token may not post a note as anyone."""
    r = client.post(path, json=_direct_body(), headers=_user_headers())
    assert r.status_code == 401
    assert llm.call_count == 0


def test_a_cross_tenant_host_is_403_on_the_direct_route(client, llm):
    r = client.post(
        DIRECT,
        json=_direct_body(),
        headers={**_headers(), "Host": "tenant-b.dodealcrm.com"},
    )
    assert r.status_code == 403
    assert llm.call_count == 0


# --- the five numbers, on this route too (register item 72) -----------------
#
# Both routes join `_judge`, and `_log_outcome` is inside it, so these fields
# cannot in principle be present on one route and absent on the other. They are
# asserted here anyway for the same reason the two routes' judgements are
# compared field for field at the top of this file: "it is the same code" is a
# claim about the tree, and the tree changes.

_TIMING_FIELDS = ("elapsed_ms", "classify_ms", "vague_ms", "score_ms", "inflight")


def _assert_numbers(line: dict, *, passes_ran: bool) -> None:
    for field in _TIMING_FIELDS:
        value = line[field]
        if value is None:
            assert not passes_ran, f"{field} is null on a scored judgement"
            continue
        assert isinstance(value, int) and not isinstance(value, bool)
        assert value >= 0


def test_a_direct_judgement_carries_the_five_numbers(client, llm, json_log):
    _script(llm)
    r = client.post(DIRECT, json=_direct_body(), headers=_headers())
    assert r.status_code == 200

    line = next(x for x in _lines(json_log) if x["message"] == "judgement_completed")
    _assert_numbers(line, passes_ran=True)


def test_both_direct_outcome_lines_say_direct(client, llm, json_log):
    """Register item 72: the route names the entry point, scored or suppressed."""
    _script(llm)
    client.post(DIRECT, json=_direct_body(), headers=_headers())
    client.post(
        DIRECT, json=_direct_body("ok", note_id=NOTE_ID + 1), headers=_headers()
    )

    routes = {
        x["message"]: x["route"]
        for x in _lines(json_log)
        if x["message"] in ("judgement_completed", "judgement_suppressed")
    }
    assert routes == {"judgement_completed": "direct", "judgement_suppressed": "direct"}


def test_the_fetch_outcome_line_says_fetch(fetch_client, llm, json_log):
    """Register item 72: the contract route names itself too."""
    _script(llm)
    fetch_client.post(JUDGE, json=_fetch_body(), headers=_user_headers())
    line = next(x for x in _lines(json_log) if x["message"] == "judgement_completed")
    assert line["route"] == "fetch"


def test_a_suppressed_direct_judgement_carries_them_too(client, json_log):
    # The length gate, on this route: nothing ran, so the three pass fields are
    # null while elapsed_ms and inflight are still numbers.
    r = client.post(DIRECT, json=_direct_body("ok"), headers=_headers())
    assert r.status_code == 200

    line = next(x for x in _lines(json_log) if x["message"] == "judgement_suppressed")
    _assert_numbers(line, passes_ran=False)
    assert line["classify_ms"] is None
    assert line["vague_ms"] is None
    assert line["score_ms"] is None


def test_the_fetch_route_carries_the_same_five(fetch_client, llm, json_log):
    _script(llm)
    fetch_client.post(JUDGE, json=_fetch_body(), headers=_user_headers())

    line = next(x for x in _lines(json_log) if x["message"] == "judgement_completed")
    _assert_numbers(line, passes_ran=True)


def test_a_request_through_http_is_counted_as_in_flight(client, llm, json_log):
    # Through the real middleware stack this time, so `inflight` is the count of
    # a request that genuinely is one: itself. Called directly (as the pipeline
    # unit tests do) the same field reads 0, and both are correct -- which is
    # what makes it worth asserting here and not only there.
    _script(llm)
    client.post(DIRECT, json=_direct_body(), headers=_headers())

    line = next(x for x in _lines(json_log) if x["message"] == "judgement_completed")
    assert line["inflight"] == 1
