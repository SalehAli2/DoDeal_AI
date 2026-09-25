"""scripts/call_e2e.py's refusal to start while other workers consume the call
queues, read from arq's health-check keys on a fakeredis client."""

from __future__ import annotations

import json

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
    for name in ("whatsapp_suggestion", "seriousness", "tags", "keywords"):
        whole = json.dumps(_EXTRAS[name], ensure_ascii=False, indent=2)
        assert f"{name}: {whole}" in lines


def test_the_report_names_a_missing_extras_part_and_no_stage2_line() -> None:
    body = {**SAMPLE, "stage2_result": None}
    lines = call_e2e.report(body, OUTCOMES[:1])
    assert "extras: None" in lines
    assert "part_reasons: None" in lines
    assert "stage2_result.reasons: None" in lines
