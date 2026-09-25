"""scripts/call_e2e.py's refusal to start while other workers consume the call
queues, read from arq's health-check keys on a fakeredis client."""

from __future__ import annotations

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
