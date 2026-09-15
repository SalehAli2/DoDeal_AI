"""Prompt templates are read once at startup, not on every model call.

Register item 85. Before this, `_load_template` read from disk inside
`build_prompt` and `with_tail` -- a synchronous read on the event loop, three to
six per judgement -- and a template missing from the image was discovered on the
first PAID call. The guards below are the halves of that: the preload refuses a
bad deployment, a judgement reads nothing, the cache dies with the app, and
everything outside a running app still reads from disk as it always did.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dodeal_ai.core import prompting
from dodeal_ai.core.auth.dependencies import get_verifier
from dodeal_ai.core.auth.verify import JwtVerifier
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.cost import limiter as cost_limiter
from dodeal_ai.core.llm import get_llm_client
from dodeal_ai.core.prompting import PromptError, build_prompt
from dodeal_ai.main import app
from dodeal_ai.tools.leads import get_leads_client
from dodeal_ai.units.structured_intelligence import state
from dodeal_ai.units.structured_intelligence.templates import UNIT_A_TEMPLATES
from tests.helpers import tokens
from tests.helpers.fake_cost_redis import FakeCostRedis
from tests.helpers.fake_leads import FakeLeadsClient, lead, note
from tests.helpers.fake_llm import FakeLLM, json_response, response
from tests.helpers.fake_operational_redis import FakeOperationalRedis

JUDGE = "/api/v1/notes/judgements"
LEAD_ID = 1656
NOTE_ID = 10
GOOD_NOTE = "Called the client, discussed the New Cairo 3BR, following up Tuesday."

# The one deliberately absent from the temporary prompts directory below. Any of
# the nine would do; naming it once keeps the assertion honest if it is renamed.
MISSING = "structured_intelligence/score_v1.txt"


@pytest.fixture
def read_counter(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every disk read `core/prompting.py` makes, by template name.

    Patches that module's ONE read site rather than Path.read_text: a global
    patch would also count reads this module never made and prove nothing.
    """
    reads: list[str] = []
    real = prompting._read_template_file

    def counted(name: str) -> str:
        reads.append(name)
        return real(name)

    monkeypatch.setattr(prompting, "_read_template_file", counted)
    return reads


# --- a missing template refuses startup -------------------------------------


def test_startup_refuses_when_one_template_is_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Eight of nine present. The process must not come up: a deployment whose
    prompts are not all there is a defect, and every judgement that named the
    missing one would 503 after paying for the calls before it."""
    for name in UNIT_A_TEMPLATES:
        if name == MISSING:
            continue
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a template", encoding="utf-8")
    monkeypatch.setattr(prompting, "_prompts_dir", lambda: tmp_path)

    with pytest.raises(PromptError) as caught, TestClient(app):
        pass

    assert MISSING in str(caught.value)
    # The message travels into a log line. It names the TEMPLATE; the resolved
    # path would carry the deployment's directory layout with it.
    assert str(tmp_path) not in str(caught.value)


def test_a_refused_startup_leaves_no_half_filled_cache(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The names that loaded before the failure must not survive it: a caller
    that caught the error would otherwise run on a cache the app never had."""
    monkeypatch.setattr(prompting, "_prompts_dir", lambda: tmp_path)
    first = tmp_path / UNIT_A_TEMPLATES[0]
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("first", encoding="utf-8")

    with pytest.raises(PromptError):
        prompting.preload_templates(UNIT_A_TEMPLATES)

    assert prompting._TEMPLATE_CACHE == {}


def test_the_tuple_names_the_nine_templates_that_ship() -> None:
    """The preload is only as good as the list it is given: a template a pass
    sends but the tuple omits would read from disk on a paid call, and one the
    tuple names but nothing ships would refuse a healthy deployment."""
    assert len(UNIT_A_TEMPLATES) == 9
    assert len(set(UNIT_A_TEMPLATES)) == 9
    for name in UNIT_A_TEMPLATES:
        assert (prompting._DEFAULT_PROMPTS_DIR / name).is_file(), name


# --- a judgement makes no disk read -----------------------------------------


@pytest.fixture
def started_client(monkeypatch: pytest.MonkeyPatch):
    """The judgement route, through a lifespan that actually ran.

    The route fixture in test_judgement_routes.py deliberately does NOT enter
    the lifespan; this one must, because the preload is the thing under test.
    """
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", tokens.TEST_SECRET)
    get_settings.cache_clear()
    test_settings = Settings(
        _env_file=None,
        jwt_signing_key=tokens.TEST_SECRET,
        jwt_algorithm=tokens.TEST_ALG,
    )

    llm = FakeLLM(
        json_response({"note_type": "discovery"}),
        # Malformed, so vague detection earns its one reprompt -- the path that
        # calls with_tail, the second place a template was read from disk.
        response("The note looks a bit thin to me."),
        json_response(
            {
                "is_vague": True,
                "missing_components": ["next_step_with_date"],
                "clarification_prompt": "Which Tuesday, and what will you cover?",
                "reasoning": "The follow-up has no date.",
            }
        ),
        json_response(
            {
                "marks": {
                    "what_happened": 20,
                    "client_said": 15,
                    "next_step_date": 15,
                    "clarity": 5,
                }
            }
        ),
    )
    leads = FakeLeadsClient(
        leads={LEAD_ID: lead(LEAD_ID)},
        notes={LEAD_ID: [note(NOTE_ID, GOOD_NOTE)]},
    )

    monkeypatch.setattr(cost_limiter, "get_cost_client", lambda: FakeCostRedis())
    monkeypatch.setattr(state, "get_operational_client", lambda: FakeOperationalRedis())

    app.dependency_overrides[get_verifier] = lambda: JwtVerifier(test_settings)
    app.dependency_overrides[get_leads_client] = lambda: leads
    app.dependency_overrides[get_llm_client] = lambda: llm

    with TestClient(app) as client:
        yield client, llm

    app.dependency_overrides.clear()
    get_settings.cache_clear()


def _headers(subdomain: str = "tenant-a", sub: int = 42) -> dict:
    return {
        "Authorization": f"Bearer {tokens.mint_token(subdomain=subdomain, sub=sub)}",
        "Host": f"{subdomain}.dodealcrm.com",
    }


def test_a_full_judgement_reads_no_template_from_disk(started_client, read_counter):
    """FOUR model calls, five templates assembled (classify, vague, vague again,
    the reprompt tail, score) and ZERO reads. The counter is installed after
    startup, so the preload's own nine reads are not what it measures."""
    client, llm = started_client

    r = client.post(
        JUDGE, json={"lead_id": LEAD_ID, "note_id": NOTE_ID}, headers=_headers()
    )

    assert r.status_code == 200, r.text
    assert llm.call_count == 4, "the scripted reprompt did not happen"
    assert read_counter == []


# --- the cache lives exactly as long as the app -----------------------------


def test_the_cache_does_not_outlive_the_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shutdown empties it, so the next app in this process reads the templates
    it was deployed with rather than the previous one's."""
    name = UNIT_A_TEMPLATES[0]

    with TestClient(app):
        assert prompting._TEMPLATE_CACHE.get(name) is not None

    assert prompting._TEMPLATE_CACHE == {}

    reads: list[str] = []
    real = prompting._read_template_file

    def counted(template_name: str) -> str:
        reads.append(template_name)
        return real(template_name)

    monkeypatch.setattr(prompting, "_read_template_file", counted)
    assert prompting._load_template(name)
    assert reads == [name]


# --- the fallback: nothing outside a running app changes --------------------


def test_build_prompt_still_reads_disk_with_no_app_started(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache is empty outside a lifespan, so `_load_template` reads exactly
    as it did before it existed. Scripts, the wheel check and every test that
    points `_prompts_dir` at a temporary directory depend on this."""
    assert prompting._TEMPLATE_CACHE == {}
    (tmp_path / "t.txt").write_text("WRITTEN HERE\n", encoding="utf-8")
    monkeypatch.setattr(prompting, "_prompts_dir", lambda: tmp_path)

    assert build_prompt("t.txt", "data").stable == "WRITTEN HERE"


def test_a_preloaded_name_is_served_from_the_cache_not_the_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the fallback: once a name is cached, changing the file
    under it changes nothing until shutdown. That is the point -- a running app
    is pinned to the templates it started with."""
    (tmp_path / "t.txt").write_text("FIRST", encoding="utf-8")
    monkeypatch.setattr(prompting, "_prompts_dir", lambda: tmp_path)
    try:
        prompting.preload_templates(["t.txt"])
        (tmp_path / "t.txt").write_text("SECOND", encoding="utf-8")
        assert prompting._load_template("t.txt") == "FIRST"
    finally:
        prompting.clear_templates()
