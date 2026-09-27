"""docker-compose.yml runs a call worker for each of the queues, on the api's
shared settings (the x-app anchor) without its port or its HEALTHCHECK. Scans
lines rather than parsing YAML, as tests/test_demo_env_stack.py does: pyyaml is
only a transitive dependency here. `docker compose config` is the hand check."""

from __future__ import annotations

import pathlib

from dodeal_ai.workers.calls import QUEUES

_COMPOSE = pathlib.Path("docker-compose.yml")


def test_each_call_queue_has_a_worker_service_running_it() -> None:
    """One service per queue, each running the calls entry point for it."""
    text = _COMPOSE.read_text(encoding="utf-8")
    for queue in QUEUES:
        assert f"  worker-{queue}:" in text, queue
        command = f'command: ["python", "-m", "dodeal_ai.workers.calls", "{queue}"]'
        assert text.count(command) == 1, queue


def test_the_workers_share_one_definition_without_the_api_healthcheck() -> None:
    """The shared block takes the app anchor and skips the api's probe."""
    text = _COMPOSE.read_text(encoding="utf-8")
    shared = text.split("worker-priority: &call-worker", 1)[1].split("\n\n", 1)[0]
    for line in ("<<: *app", "disable: true"):
        assert line in shared, line
    assert text.count("<<: *call-worker") == len(QUEUES) - 1


def test_no_worker_publishes_the_api_port() -> None:
    """Only the api publishes 8000: nothing a worker is built from names a
    port, and no service extends the api, whose port `ports: !reset []` did
    not drop through a merge key (docker compose config showed three workers
    on 8000)."""
    text = _COMPOSE.read_text(encoding="utf-8")
    app = text.split("x-app: &app", 1)[1].split("\n\n", 1)[0]
    shared = text.split("worker-priority: &call-worker", 1)[1].split("\n\n", 1)[0]
    assert "ports" not in app and "ports" not in shared
    assert not [line for line in text.splitlines() if line.strip() == "extends:"]
    assert text.count('ports: ["8000:8000"]') == 1
    api = text.split("  api:", 1)[1].split("\n\n", 1)[0]
    assert 'ports: ["8000:8000"]' in api
