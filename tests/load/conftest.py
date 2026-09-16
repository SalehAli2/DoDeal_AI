"""The load lane: the whole app, in process, under concurrent bursts, on a real Redis.

Run with `pytest -m load --no-cov`. pyproject's addopts exclude the marker from
the default run, and the hook below puts it on every test in this directory by
PATH, the way tests/redis_real does.

THE REAL-REDIS LANE'S USABILITY RULES, imported rather than restated.
DODEAL_REDIS_REAL_URL unset, a URL that selects db0 to db2, or a server that does
not answer PING skips every test, and fails it when DODEAL_REDIS_REAL_REQUIRED
is set. The reason names an error TYPE and never the URL.

THE SERVICE'S OWN FACTORIES, ON TWO LANE DATABASES. No factory is patched. The
two service URLs point at db 10 (cost) and db 11 (operational) on the lane's
server, and `get_cost_client` / `get_operational_client` build real pools from
them. Settings, both client caches and both breakers are cleared before and
after each test, so no pool outlives the event loop that opened it. The root
conftest installs no fakes for this lane and does not count its pool builds.

BOTH DATABASES ARE FLUSHED before each test and again after it. They belong to
the lane, so point DODEAL_REDIS_REAL_URL at a server whose db 10 and db 11 hold
nothing you want to keep.

THE APP IS SERVED IN PROCESS through httpx.ASGITransport, so a burst is
`asyncio.gather` over real concurrent requests on one event loop. No lifespan
runs. The leads client (the vendored tenant-a corpus) and the LLM (FakeLLM,
scripted by template) are dependency overrides, as in the route tests.
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
import pytest
import pytest_asyncio
import redis
from redis import asyncio as redis_async
from redis.connection import parse_url

from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.breaker import reset_breakers
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.redis import get_cost_client, get_operational_client
from dodeal_ai.main import app
from dodeal_ai.schemas.lead import LeadNote
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence.classify import CLASSIFY_TEMPLATE
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
from dodeal_ai.units.structured_intelligence.schemas import NoteType
from dodeal_ai.units.structured_intelligence.scoring import SCORE_TEMPLATE
from dodeal_ai.units.structured_intelligence.vague import template_for
from tests.helpers import tokens
from tests.helpers.fake_leads import FakeLeadsClient, load_fixture_client
from tests.helpers.fake_llm import FakeLLM, json_response
from tests.redis_real.conftest import (
    _DEFAULTS,
    _SERVICE_DBS,
    URL_VAR,
    _unusable,
)

_LANE_DIR = pathlib.Path(__file__).parent

# The lane's two databases. Outside db0 to db2 (the service's own) and apart
# from db 9, which the real-Redis lane uses, so a flush here reaches neither.
COST_DB = 10
OPERATIONAL_DB = 11

TENANT = "tenant-a"
SUBJECT = 42
HOST = f"{TENANT}.dodealcrm.com"
JUDGE = "/api/v1/notes/judgements"
DIRECT = "/api/v1/notes/judgements/direct"

# Gate 4's per-user request counter for the one subject every lane token names.
USER_COST_KEY = f"cost:user:{TENANT}:{SUBJECT}"

# One judgement's three answers. The marks sum to 55 of 80 (69, accept with a
# flag), so a judgement with a clarification prompt sends it unless withheld.
CLASSIFY_AS = NoteType.DISCOVERY
VAGUE_ANSWER = {
    "is_vague": True,
    "missing_components": ["next_step_with_date"],
    "clarification_prompt": "Which day is the follow-up, and what will it cover?",
    "reasoning": "The follow-up has no date.",
}
SCORE_ANSWER = {
    "marks": {
        "what_happened": 20,
        "client_said": 15,
        "next_step_date": 15,
        "clarity": 5,
    }
}


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test under this directory `load`, before `-m` deselects."""
    for item in items:
        if item.path.is_relative_to(_LANE_DIR):
            item.add_marker(pytest.mark.load)


def _on_db(url: str, db: int) -> str:
    """`url` with its database replaced by `db`; scheme, credentials and host kept.

    A `db` query parameter is dropped too, because redis-py prefers it to the path.
    """
    parts = urlsplit(url)
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if k != "db"])
    return urlunsplit(parts._replace(path=f"/{db}", query=query))


@dataclass(frozen=True)
class Stores:
    """The lane's own clients on db 10 and db 11, for flushing and for reading back
    what the app wrote. Never the app's clients, and never handed to the app."""

    cost: redis_async.Redis
    operational: redis_async.Redis
    cost_url: str
    operational_url: str


def _client(url: str) -> redis_async.Redis:
    """A lane client with Settings' default socket budgets, so a dead URL skips fast."""
    return redis_async.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=_DEFAULTS.redis_connect_timeout_seconds,
        socket_timeout=_DEFAULTS.redis_socket_timeout_seconds,
    )


@pytest_asyncio.fixture
async def stores() -> AsyncIterator[Stores]:
    """Both lane databases, usable and empty, or the test is skipped (or failed)."""
    url = os.environ.get(URL_VAR)
    if not url:
        _unusable("DODEAL_REDIS_REAL_URL not set; the load lane needs a live server")

    # The real-Redis lane's refusal, applied to the URL as given: a URL that names
    # a service database is a misconfiguration, whichever databases we then use.
    db = int(parse_url(url).get("db", 0))
    if db in _SERVICE_DBS:
        _unusable(
            f"{URL_VAR} selects db{db}; the load lane refuses db0 to db2, "
            "the service's own stores"
        )

    cost = _client(_on_db(url, COST_DB))
    operational = _client(_on_db(url, OPERATIONAL_DB))
    try:
        try:
            await cost.ping()
        except redis.RedisError as exc:
            _unusable(
                f"{URL_VAR} is set but PING failed ({type(exc).__name__}); "
                "the load lane needs a live server"
            )
        await cost.flushdb()
        await operational.flushdb()
        yield Stores(
            cost=cost,
            operational=operational,
            cost_url=_on_db(url, COST_DB),
            operational_url=_on_db(url, OPERATIONAL_DB),
        )
        await cost.flushdb()
        await operational.flushdb()
    finally:
        await cost.aclose()
        await operational.aclose()


@pytest.fixture
def operational_url(stores: Stores) -> str:
    """The URL the app's operational client is built from. A module overrides this
    fixture to serve the app on a store that is not there."""
    return stores.operational_url


@pytest.fixture
def usable_notes() -> list[tuple[int, LeadNote]]:
    """One note per lead from the vendored corpus that passes the length gate, as
    (lead_id, note), in the corpus's own lead order."""
    config = get_tenant_config(TENANT)
    found = []
    for lead_id, notes in load_fixture_client().notes.items():
        for note in notes:
            text = note.note.strip()
            if (
                config.min_note_chars <= len(text) <= config.max_note_chars
                and len(text.split()) >= config.min_note_tokens
            ):
                found.append((lead_id, note))
                break
    return found


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain=TENANT, sub=SUBJECT)}",
        "Host": HOST,
    }


@dataclass
class Lane:
    """The served app and the fakes behind it, with the lane's two stores."""

    user_cost_key: ClassVar[str] = USER_COST_KEY

    client: httpx.AsyncClient
    llm: FakeLLM
    leads: FakeLeadsClient
    stores: Stores
    notes: list[tuple[int, LeadNote]] = field(default_factory=list)

    def script(self, judgements: int) -> None:
        """Queue `judgements` full judgements' worth of answers, by template."""
        self.llm.script_for(
            CLASSIFY_TEMPLATE,
            *[json_response({"note_type": CLASSIFY_AS.value})] * judgements,
        )
        self.llm.script_for(
            template_for(CLASSIFY_AS), *[json_response(VAGUE_ANSWER)] * judgements
        )
        self.llm.script_for(SCORE_TEMPLATE, *[json_response(SCORE_ANSWER)] * judgements)

    async def judge(self, lead_id: int, note_id: int) -> httpx.Response:
        """One request on the fetch route, as the CRM would send it."""
        return await self.client.post(
            JUDGE, json={"lead_id": lead_id, "note_id": note_id}, headers=_headers()
        )

    async def judge_direct(
        self, lead_id: int, note: LeadNote, text: str
    ) -> httpx.Response:
        """One request on the direct route, carrying `text` for `note`'s id."""
        body = {
            "lead_id": lead_id,
            "note_id": note.id,
            "author_id": note.author_id,
            "note_text": text,
            "lead": {
                "leadType": None,
                "enquiryType": None,
                "project": None,
                "status": None,
            },
        }
        return await self.client.post(DIRECT, json=body, headers=_headers())


async def _close_service_clients() -> None:
    """Close whichever factory clients the test built, then drop both caches."""
    for factory in (get_cost_client, get_operational_client):
        if factory.cache_info().currsize:
            await factory().aclose(close_connection_pool=True)
        factory.cache_clear()


@pytest_asyncio.fixture
async def lane(
    monkeypatch: pytest.MonkeyPatch,
    stores: Stores,
    operational_url: str,
    usable_notes: list[tuple[int, LeadNote]],
) -> AsyncIterator[Lane]:
    """The app served in process on the lane's databases, with fresh fakes."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    monkeypatch.setenv("DODEAL_REDIS_COST_URL", stores.cost_url)
    monkeypatch.setenv("DODEAL_REDIS_OPERATIONAL_URL", operational_url)
    get_settings.cache_clear()
    await _close_service_clients()
    reset_breakers()

    verifier_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )
    llm = FakeLLM()
    leads = load_fixture_client()
    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(verifier_settings)
    app.dependency_overrides[get_leads_client] = lambda: leads
    app.dependency_overrides[get_llm_client] = lambda: llm
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=f"http://{HOST}"
        ) as client:
            yield Lane(
                client=client, llm=llm, leads=leads, stores=stores, notes=usable_notes
            )
    finally:
        app.dependency_overrides.clear()
        await _close_service_clients()
        reset_breakers()
        get_settings.cache_clear()
