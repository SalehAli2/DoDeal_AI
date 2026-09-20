"""The body-size limit: what it refuses, how cheaply, and what it lets through.

Register item 87. Two kinds of test, and the split is deliberate.

The RAW ASGI tests drive `BodyLimitMiddleware` directly, because the claims
they make are about the middleware and nothing else -- that `receive` is never
called on the header path, that a non-http scope is untouched. A FastAPI app
between the assertion and the thing asserted would only add ways for the test
to be wrong.

The TESTCLIENT tests go through `dodeal_ai.main.app` with the real cap, because
the claims they make are about the WHOLE STACK: that exactly 64 kB still
reaches a route and is judged, that 64 kB + 1 does not, and that a streamed
body is refused with 413 rather than with FastAPI's own 400 for an
unparseable one. That last one is the reason the middleware sends its own
response, and only a real FastAPI route exercises it.

WHY THE 413 PHRASE IS COMPUTED AND NOT SPELLED OUT. `_unit_error_body` builds
`detail` from `HTTPStatus(413).phrase`, and the stdlib renamed that phrase from
"Request Entity Too Large" to "Content Too Large" in 3.13. `requires-python` is
">=3.12", so a literal here would pin the suite to one interpreter over a
string this service does not choose. Every other field IS spelled out.
"""

from __future__ import annotations

import json
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.main import app
from dodeal_ai.middleware import body_limit
from dodeal_ai.middleware.body_limit import BodyLimitMiddleware
from dodeal_ai.tools.leads import get_leads_client
from tests.helpers import tokens
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.helpers.score_answers import score_payload

# The real default, asserted against Settings below rather than trusted: the
# 64 kB in the register is the number these tests are about, and a test that
# quietly followed the field if somebody halved it would stop being that test.
REAL_CAP = 65_536

# The raw-ASGI tests use a cap small enough to write a body for by hand. It
# goes through a real Settings object so nothing here invents a shape config
# does not have.
SMALL_CAP = 64

JUDGE = "/api/v1/notes/judgements"
LEAD_ID = 1656
NOTE_ID = 10
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."

TOO_LARGE_PHRASE = HTTPStatus(413).phrase


def test_the_default_cap_is_the_64_kb_the_register_names() -> None:
    """The number every other test in this file is written against."""
    settings = Settings(_env_file=None, jwt_signing_key="test-key")
    assert settings.max_request_body_bytes == REAL_CAP


def test_a_cap_of_zero_is_refused_at_startup() -> None:
    """ge=1. A cap of zero makes every judgement a 413 -- an outage that looks
    exactly like the CRM sending bad requests, and so the worst kind to find."""
    with pytest.raises(ValueError):
        Settings(_env_file=None, jwt_signing_key="test-key", max_request_body_bytes=0)


# --- raw ASGI: the middleware on its own ------------------------------------


@pytest.fixture
def _small_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cap of SMALL_CAP, read through a real Settings object, so nothing here
    reaches a developer's environment for the number."""
    monkeypatch.setattr(
        body_limit,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            jwt_signing_key="test-key",
            max_request_body_bytes=SMALL_CAP,
        ),
    )


class Recorder:
    """An inner ASGI app that records that it was entered and answers 200."""

    def __init__(self) -> None:
        self.entries = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.entries += 1
        if scope["type"] == "http":
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})


class BodyReader(Recorder):
    """An inner ASGI app that DRAINS the request body, the way a route does.

    It lets whatever `receive` raises out rather than swallowing it -- which is
    what a plain ASGI app does and what FastAPI notably does not, and the two
    together are why the middleware does not delegate its refusal.
    """

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.entries += 1
        more = True
        while more:
            message = await receive()
            more = message.get("more_body", False)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def _http_scope(**extra) -> Scope:
    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/anything",
        "headers": [],
        "app": FastAPI(),
    }
    scope.update(extra)
    return scope


def _chunks(*bodies: bytes) -> tuple[Receive, list[str]]:
    """A `receive` that hands over `bodies` in order, and the log of its calls."""
    calls: list[str] = []
    remaining = list(bodies)

    async def receive() -> Message:
        calls.append("receive")
        if remaining:
            body = remaining.pop(0)
            return {
                "type": "http.request",
                "body": body,
                "more_body": bool(remaining),
            }
        return {"type": "http.disconnect"}

    return receive, calls


def _collect() -> tuple[Send, list[Message]]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    return send, messages


def _status(messages: list[Message]) -> int:
    return next(m for m in messages if m["type"] == "http.response.start")["status"]


def _body(messages: list[Message]) -> bytes:
    return b"".join(
        m.get("body", b"") for m in messages if m["type"] == "http.response.body"
    )


async def test_a_declared_length_over_the_cap_never_touches_the_body(_small_cap):
    """THE POINT OF THE PIECE. The refusal costs a header read: `receive` is not
    called once, so not a byte of the body comes off the socket, and the inner
    app is never entered."""
    inner = BodyReader()
    receive, calls = _chunks(b"x" * (SMALL_CAP + 1))
    send, messages = _collect()
    scope = _http_scope(headers=[(b"content-length", str(SMALL_CAP + 1).encode())])

    await BodyLimitMiddleware(inner)(scope, receive, send)

    assert _status(messages) == 413
    assert calls == []
    assert inner.entries == 0


async def test_the_refusal_body_is_the_shared_error_shape(_small_cap):
    """Not a second spelling of the error body -- the same builder every
    enumerated refusal uses, so a CRM branches on one `reason` field."""
    receive, _ = _chunks(b"x" * (SMALL_CAP + 1))
    send, messages = _collect()
    scope = _http_scope(headers=[(b"content-length", str(SMALL_CAP + 1).encode())])

    await BodyLimitMiddleware(Recorder())(scope, receive, send)

    assert json.loads(_body(messages)) == {
        "detail": TOO_LARGE_PHRASE,
        "reason": "payload_too_large",
        "request_id": "unknown",
    }


async def test_the_header_path_request_id_is_unknown_by_placement(_small_cap):
    """ "unknown" is correct and not a defect: outermost, no id has been minted
    yet. The streamed test below is the one that carries a real one."""
    receive, _ = _chunks(b"x" * (SMALL_CAP + 1))
    send, messages = _collect()
    scope = _http_scope(headers=[(b"content-length", str(SMALL_CAP + 1).encode())])

    await BodyLimitMiddleware(Recorder())(scope, receive, send)

    assert json.loads(_body(messages))["request_id"] == "unknown"


async def test_a_body_of_exactly_the_cap_is_admitted(_small_cap):
    """`>` and not `>=`: the cap is the largest body ALLOWED."""
    inner = BodyReader()
    receive, _ = _chunks(b"x" * SMALL_CAP)
    send, messages = _collect()
    scope = _http_scope(headers=[(b"content-length", str(SMALL_CAP).encode())])

    await BodyLimitMiddleware(inner)(scope, receive, send)

    assert _status(messages) == 200
    assert inner.entries == 1


async def test_a_streamed_body_is_counted_across_messages(_small_cap):
    """No `content-length` at all: the count is what holds, and it is a running
    total across every `http.request` message rather than a per-message check."""
    inner = BodyReader()
    receive, _ = _chunks(*([b"x" * 20] * 4))  # 80 bytes in four 20-byte messages
    send, messages = _collect()

    await BodyLimitMiddleware(inner)(_http_scope(), receive, send)

    assert _status(messages) == 413


async def test_a_streamed_body_stops_being_read_at_the_byte_it_overflows(_small_cap):
    """Nothing is buffered and nothing is drained after the refusal: the fourth
    20-byte message is the one that passes 64, and the fifth is never asked
    for."""
    receive, calls = _chunks(*([b"x" * 20] * 10))
    send, _ = _collect()

    await BodyLimitMiddleware(BodyReader())(_http_scope(), receive, send)

    assert len(calls) == 4


async def test_a_lying_content_length_does_not_buy_a_larger_body(_small_cap):
    """The header is the caller's claim about itself. It can bound the cheap
    path; it can never be the only check."""
    receive, _ = _chunks(b"x" * (SMALL_CAP + 1))
    send, messages = _collect()
    scope = _http_scope(headers=[(b"content-length", b"1")])

    await BodyLimitMiddleware(BodyReader())(scope, receive, send)

    assert _status(messages) == 413


@pytest.mark.parametrize("value", [b"not-a-number", b"-1", b"12, 34", b""])
async def test_an_unparseable_content_length_falls_to_the_count(_small_cap, value):
    """Junk in the header is not a free pass and is not a crash: it is ignored,
    and the streamed count refuses the body on its real size."""
    receive, _ = _chunks(b"x" * (SMALL_CAP + 1))
    send, messages = _collect()
    scope = _http_scope(headers=[(b"content-length", value)])

    await BodyLimitMiddleware(BodyReader())(scope, receive, send)

    assert _status(messages) == 413


async def test_the_first_content_length_header_wins(_small_cap):
    """A caller who sends the header twice does not get to choose which copy is
    checked -- the same rule request_id.py applies to its own header."""
    receive, calls = _chunks(b"x" * (SMALL_CAP + 1))
    send, messages = _collect()
    scope = _http_scope(
        headers=[
            (b"content-length", str(SMALL_CAP + 1).encode()),
            (b"content-length", b"1"),
        ]
    )

    await BodyLimitMiddleware(Recorder())(scope, receive, send)

    assert _status(messages) == 413
    assert calls == []  # refused on the first header, not on the count


async def test_an_empty_body_passes_through(_small_cap):
    """A GET carries no body, so the guard costs it one dict lookup."""
    inner = BodyReader()
    receive, _ = _chunks(b"")
    send, messages = _collect()

    await BodyLimitMiddleware(inner)(_http_scope(method="GET"), receive, send)

    assert _status(messages) == 200
    assert inner.entries == 1


# --- non-http scopes ---------------------------------------------------------

_NON_HTTP_SCOPES = [
    pytest.param({"type": "lifespan", "app": FastAPI()}, id="lifespan"),
    pytest.param(
        {"type": "websocket", "path": "/ws", "headers": [], "app": FastAPI()},
        id="websocket",
    ),
]


@pytest.mark.parametrize("scope", _NON_HTTP_SCOPES)
async def test_a_non_http_scope_reaches_the_inner_app_untouched(_small_cap, scope):
    """The same scope object, with nothing added to it, and the same `receive`:
    a lifespan has no body to bound and no 413 to be sent."""
    inner = Recorder()
    before = dict(scope)
    receive, calls = _chunks()
    send, _ = _collect()

    await BodyLimitMiddleware(inner)(scope, receive, send)

    assert inner.entries == 1
    assert scope == before
    assert calls == []


# --- the whole stack, at the real cap ---------------------------------------


@pytest.fixture
def leads() -> FakeLeadsClient:
    return FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE)]},
    )


@pytest.fixture
def llm() -> FakeLLM:
    """One judgement's three answers, in the order the pipeline issues them."""
    return FakeLLM(
        json_response({"note_type": "discovery"}),
        json_response(
            {
                "is_vague": True,
                "missing_components": ["next_step_with_date"],
                "clarification_prompt": "Which Tuesday, and what will you cover?",
                "reasoning": "The follow-up has no date.",
            }
        ),
        json_response(score_payload()),
    )


@pytest.fixture
def client(monkeypatch, leads, llm):
    """main.py's real app and its real cap, with the three seams faked. The
    gate-chain wiring is test_judgement_routes.py's, unchanged."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_leads_client] = lambda: leads
    app.dependency_overrides[get_llm_client] = lambda: llm

    yield TestClient(app)

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers(subdomain: str = "tenant-a", sub: int = 42) -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain=subdomain, sub=sub)}",
        "Host": f"{subdomain}.dodealcrm.com",
        "Content-Type": "application/json",
    }


def _padded_body(size: int) -> bytes:
    """A valid judgement request padded with insignificant JSON whitespace to
    exactly `size` bytes.

    Padding rather than a long field value because JudgementRequest is
    `extra="forbid"` and carries two integers: there is no field to make big,
    which is the schema working. Whitespace between tokens is JSON, so what
    reaches the route is the same request either way -- the only thing that
    changes is its size on the wire, which is the one thing under test.
    """
    head = b'{"lead_id": %d, "note_id": %d' % (LEAD_ID, NOTE_ID)
    padding = size - len(head) - 1
    assert padding >= 0, "size is smaller than the shortest valid body"
    return head + b" " * padding + b"}"


def test_a_body_of_exactly_the_cap_is_judged(client):
    """64 kB exactly, all the way through the pipeline to a judgement."""
    payload = _padded_body(REAL_CAP)
    assert len(payload) == REAL_CAP

    r = client.post(JUDGE, content=payload, headers=_headers())

    assert r.status_code == 200
    assert r.json()["note_id"] == NOTE_ID


def test_one_byte_over_the_cap_is_413(client):
    """The same request, one byte of whitespace longer."""
    payload = _padded_body(REAL_CAP + 1)
    assert len(payload) == REAL_CAP + 1

    r = client.post(JUDGE, content=payload, headers=_headers())

    assert r.status_code == 413
    assert r.json() == {
        "detail": TOO_LARGE_PHRASE,
        "reason": "payload_too_large",
        "request_id": "unknown",
    }


def test_an_oversized_body_is_refused_before_gate_1(client):
    """No token at all, and the answer is 413 rather than 401: the bytes are
    refused before anything asks who sent them. That IS the ordering claim."""
    r = client.post(
        JUDGE,
        content=_padded_body(REAL_CAP + 1),
        headers={"Host": "tenant-a.dodealcrm.com", "Content-Type": "application/json"},
    )

    assert r.status_code == 413
    assert r.json()["reason"] == "payload_too_large"


def test_an_oversized_body_spends_nothing(client, llm, leads):
    """Nothing was fetched and nothing was asked of a model."""
    client.post(JUDGE, content=_padded_body(REAL_CAP + 1), headers=_headers())

    assert llm.call_count == 0
    assert leads.calls == []


def test_a_streamed_oversized_body_is_413_and_not_fastapis_400(client):
    """THE REASON THE MIDDLEWARE SENDS ITS OWN RESPONSE.

    A generator body makes httpx send chunked with no `content-length`, so the
    refusal happens inside the route's own `await request.body()`. FastAPI
    catches every exception from that call and re-raises it as
    `HTTPException(400, "There was an error parsing the body")` -- so a
    middleware that merely raised would tell the caller its request was
    MALFORMED when it was only too big. Run against that design this assertion
    reads 400.
    """

    def chunked():
        payload = _padded_body(REAL_CAP + 1)
        for start in range(0, len(payload), 8192):
            yield payload[start : start + 8192]

    r = client.post(JUDGE, content=chunked(), headers=_headers())

    assert r.status_code == 413
    assert r.json()["reason"] == "payload_too_large"


def test_the_streamed_refusal_carries_a_real_request_id(client):
    """The consequence of WHERE it refuses, not of a second policy: by the time
    a route is reading a body, RequestIDMiddleware has run and written the id
    into the scope this middleware reads back."""

    def chunked():
        payload = _padded_body(REAL_CAP + 1)
        for start in range(0, len(payload), 8192):
            yield payload[start : start + 8192]

    r = client.post(JUDGE, content=chunked(), headers=_headers())

    request_id = r.json()["request_id"]
    assert request_id != "unknown"
    assert len(request_id) == len("00000000-0000-0000-0000-000000000000")


def test_health_carries_no_body_and_is_not_exempt(client):
    """Unlike inflight.py, /health is NOT exempted -- it carries no body, so the
    check costs it nothing, and an exemption would be a path a caller can aim a
    large body at.

    Asserted as the ABSENCE of an exemption list as well as the 200: a /health
    that answers 200 proves only that the guard let it through, which it would
    do either way. There must be no path this middleware skips."""
    assert client.get("/health").status_code == 200
    assert not hasattr(body_limit, "EXEMPT_PATHS")


def test_an_ordinary_sized_request_is_unaffected(client):
    """The control: the same route, a normal body, still judged."""
    r = client.post(
        JUDGE, json={"lead_id": LEAD_ID, "note_id": NOTE_ID}, headers=_headers()
    )

    assert r.status_code == 200
