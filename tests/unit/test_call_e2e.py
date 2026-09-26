"""scripts/call_e2e.py's refusal to start while other workers consume the call
queues, read from arq's health-check keys on a fakeredis client; its report;
and the --vocabulary and --dialect flags in the unit_b PUT."""

from __future__ import annotations

import copy
import json
from html.parser import HTMLParser
from pathlib import Path

import fakeredis
import pytest

from scripts import call_e2e


def test_no_health_check_key_is_no_busy_queue() -> None:
    client = fakeredis.FakeRedis()
    client.set("arq:queue:health-check", "another app's worker")
    assert call_e2e.busy_queues(client) == []
    call_e2e.refuse_other_workers(client)


def test_a_worker_on_a_call_queue_is_refused_and_named() -> None:
    client = fakeredis.FakeRedis()
    client.set("arq:calls:stage2:health-check", "j_complete=0")
    client.set("arq:calls:normal:health-check", "j_complete=3")
    assert call_e2e.busy_queues(client) == ["arq:calls:normal", "arq:calls:stage2"]
    with pytest.raises(SystemExit) as refused:
        call_e2e.refuse_other_workers(client)
    assert str(refused.value) == (
        "call_e2e: refused: other workers are consuming arq:calls:normal, "
        "arq:calls:stage2 (arq health-check keys present). Stop them first; "
        "a killed worker's key expires within an hour."
    )


def test_every_call_queue_is_watched() -> None:
    client = fakeredis.FakeRedis()
    for queue in ("priority", "normal", "overnight", "stage2"):
        client.set(f"arq:calls:{queue}:health-check", "up")
    assert call_e2e.busy_queues(client) == [
        "arq:calls:priority",
        "arq:calls:normal",
        "arq:calls:overnight",
        "arq:calls:stage2",
    ]


# --- the report ------------------------------------------------------------------

# An invented call's status body, shaped as GET /api/v1/calls/jobs/{job_id} is.
_EXTRAS = {
    "keywords": [
        {
            "kind": "community",
            "said": "the marina",
            "english": "Marina",
            "canonical": None,
            "segment": "s2",
        }
    ],
    "tags": {"outcome": "moved_forward", "stage": "viewing", "client_type": "end_user"},
    "agent_dialect": {"dialect": "unknown", "quote": None, "segment": None},
    "whatsapp_dialect": None,
    "whatsapp_suggestion": {"language": "en", "text": "See you on Tuesday at four."},
    "seriousness": {
        "band": "medium",
        "yes": 1,
        "manager_only": True,
        "checks": {
            "budget_given": {
                "answer": "yes",
                "reason": "gave a budget",
                "quote": "my budget is two million",
                "segment": "s2",
            }
        },
    },
}
SAMPLE: dict = {
    "job_id": "job-1",
    "status": "done",
    "reason": None,
    "delivery": "delivered",
    "stage2": "done",
    "result": {
        "stage": 1,
        "call_id": "call-1",
        "duration_seconds": 20,
        "transcript": {
            "segments": [
                {
                    "start_s": 0.0,
                    "end_s": 4.0,
                    "speaker": "agent",
                    "text": "Good morning, about the villa <b>viewing</b>.",
                    "language": "en",
                    "confidence": 0.9,
                },
                {
                    "start_s": 4.5,
                    "end_s": 9.0,
                    "speaker": "client",
                    "text": "My budget is two million, near the marina.",
                    "language": "en",
                    "confidence": 0.8,
                },
            ],
            "language_profile": "mostly_en",
            "uncertain": False,
            "provider": "fake",
            "model": "fake",
            "uncertain_reasons": [],
        },
        "roles": {"speakers": None, "applied": True, "reasons": []},
        "signals": {"version": "call_signals_v2", "talk_balance": "balanced"},
        "analysis": {
            "language": "en",
            "uncertain": False,
            "summary": "The client wants a villa near the marina.",
            "elements": {"discussed": ["budget"]},
            "details": {
                "budget": {
                    "value": "2,000,000 AED",
                    "state": "stated",
                    "quote": "My budget is two million",
                    "segment": "s2",
                }
            },
            "mood": {"value": "positive", "uncertain": False},
            "crm_note": "Budget two million; viewing Tuesday.",
        },
        "analysis_reason": None,
        "versions": {"prompt": "unit_b_prompts_v7"},
    },
    "stage2_result": {
        "stage": 2,
        "call_id": "call-1",
        "objections": {"raised": 0, "addressed": 0, "satisfied": 0, "items": []},
        "score": None,
        "escalations": [],
        "coaching": {"language": "en", "observations": [], "plan": ["Ask early."]},
        "extras": _EXTRAS,
        "reasons": {"score": "too_short"},
        "versions": {"prompt": "unit_b_prompts_v7"},
    },
    "translations": None,
}
OUTCOMES = [
    {"message": "call_job_outcome", "pass_tokens": {"extract": 10}},
    {"message": "call_stage2_outcome", "part_reasons": {"score": "too_short"}},
]


def test_the_report_prints_the_reasons_and_the_whole_extras_part() -> None:
    failed = {**SAMPLE, "result": {**SAMPLE["result"], "analysis": None}}
    failed["result"]["analysis_reason"] = "extract_malformed_output"

    lines = call_e2e.report(failed, OUTCOMES)

    assert "analysis_reason: extract_malformed_output" in lines
    shown = json.dumps({"score": "too_short"}, indent=2)
    assert f"part_reasons: {shown}" in lines
    assert f"stage2_result.reasons: {shown}" in lines
    names = ("whatsapp_suggestion", "agent_dialect", "seriousness", "tags", "keywords")
    for name in names:
        whole = json.dumps(_EXTRAS[name], ensure_ascii=False, indent=2)
        assert f"{name}: {whole}" in lines
    assert "whatsapp_dialect: None" in lines


def test_the_report_names_a_missing_extras_part_and_no_stage2_line() -> None:
    body = {**SAMPLE, "stage2_result": None}
    lines = call_e2e.report(body, OUTCOMES[:1])
    assert "extras: None" in lines
    assert "part_reasons: None" in lines
    assert "stage2_result.reasons: None" in lines


# --- the page --------------------------------------------------------------------


class _Page(HTMLParser):
    """Collects the tags, the attributes and the embedded status JSON."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[str] = []
        self.attrs: list[str] = []
        self.embedded = ""
        self._in_json = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        self.tags.append(tag)
        self.attrs += [name for name, _ in attrs]
        self._in_json = ("id", "status-json") in attrs

    def handle_endtag(self, tag: str) -> None:
        self._in_json = False

    def handle_data(self, data: str) -> None:
        if self._in_json:
            self.embedded += data


def _parsed(page: str) -> _Page:
    parser = _Page()
    parser.feed(page)
    parser.close()
    return parser


def test_the_page_renders_a_sample_payload() -> None:
    """The guard: every section, the score marked LOCAL TEST, the transcript
    as bubbles, and the status JSON embedded so that it parses back."""
    page = call_e2e.report_html(SAMPLE, OUTCOMES)
    parsed = _parsed(page)

    assert json.loads(parsed.embedded) == SAMPLE
    for title in (
        "Status",
        "Reasons",
        "Summary",
        "Details",
        "Signals",
        "Roles",
        "Objections",
        "Score",
        "Coaching",
        "Extras",
        "Transcript",
    ):
        assert f"<h2>{title}</h2>" in page
    assert "LOCAL TEST" in page
    assert page.count('class="bubble agent"') == 1
    assert page.count('class="bubble client"') == 1
    assert '<span class="state">stated</span>' in page
    assert "See you on Tuesday at four." in page
    assert "too_short" in page


def test_the_page_escapes_the_call_and_asks_the_network_for_nothing() -> None:
    page = call_e2e.report_html(SAMPLE, OUTCOMES)
    parsed = _parsed(page)

    assert "&lt;b&gt;viewing&lt;/b&gt;" in page
    assert "b" not in parsed.tags
    assert not {"src", "href"} & set(parsed.attrs)
    assert "url(" not in page and "@import" not in page
    assert set(parsed.tags) <= {
        "html", "head", "meta", "title", "style", "body", "main", "h1", "h2",
        "section", "p", "dl", "dt", "dd", "ul", "li", "span", "table", "tr",
        "th", "td", "div", "details", "summary", "pre", "script",
    }  # fmt: skip


def test_a_closing_script_tag_in_the_call_cannot_end_the_embedded_json() -> None:
    body = copy.deepcopy(SAMPLE)
    body["result"]["transcript"]["segments"][0]["text"] = "</script><script>x()"
    parsed = _parsed(call_e2e.report_html(body, []))
    assert json.loads(parsed.embedded) == body
    assert parsed.tags.count("script") == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"status": "failed", "result": None, "stage2_result": None},
        {"result": {"transcript": {"segments": ["loose"]}, "analysis": []}},
        {"result": {"analysis": {"details": {"budget": "flat"}}}},
    ],
)
def test_the_page_renders_a_partial_or_odd_payload(body: dict) -> None:
    parsed = _parsed(call_e2e.report_html(body, []))
    assert json.loads(parsed.embedded) == body


# --- --vocabulary and --dialect ------------------------------------------------------

CALLBACK = "http://127.0.0.1:9/callback"


@pytest.fixture
def parse(monkeypatch):
    """main's parse without the run: the flags as run() gets them."""
    seen: list[object] = []
    monkeypatch.setattr(call_e2e, "run", lambda args: seen.append(args) or 0)

    def parsed(*flags: str) -> object:
        call_e2e.main(["call.wav", *flags])
        return seen[-1]

    return parsed


def test_the_vocabulary_is_one_term_per_line_in_utf8(tmp_path: Path, parse) -> None:
    listed = tmp_path / "vocabulary.txt"
    listed.write_bytes(
        "\ufeffPalm Grove\n\n  واحة النخيل  \r\nMarina Heights\n".encode()
    )
    terms = call_e2e.vocabulary_terms(listed)
    assert terms == ["Palm Grove", "واحة النخيل", "Marina Heights"]
    section = call_e2e.unit_b_section(parse(), CALLBACK, terms)
    assert section["keyword_vocabulary"] == terms
    assert "whatsapp_default_dialect" not in section


def test_a_vocabulary_inside_the_repo_or_not_utf8_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="inside this repository"):
        call_e2e.vocabulary_terms(Path("README.md"))
    latin = tmp_path / "latin1.txt"
    latin.write_bytes("Caf\xe9 Towers\n".encode("latin-1"))
    with pytest.raises(SystemExit, match="could not be read as UTF-8"):
        call_e2e.vocabulary_terms(latin)
    with pytest.raises(SystemExit, match="could not be read as UTF-8"):
        call_e2e.vocabulary_terms(tmp_path / "missing.txt")


def test_the_dialect_flag_sets_the_default_dialect_for_the_run(parse) -> None:
    section = call_e2e.unit_b_section(parse("--dialect", "egyptian_ar"), CALLBACK, None)
    assert section["whatsapp_default_dialect"] == "egyptian_ar"
    assert "keyword_vocabulary" not in section
    with pytest.raises(SystemExit):
        parse("--dialect", "gulf")


def test_without_the_flags_the_put_is_unchanged(parse) -> None:
    assert call_e2e.unit_b_section(parse(), CALLBACK, None) == {
        "calls_enabled": True,
        "audio_hosts": ["127.0.0.1"],
        "callback_url": CALLBACK,
        "number_detection_enabled": True,
        "alarm_phrases_enabled": True,
        "scoring_enabled": True,
    }
