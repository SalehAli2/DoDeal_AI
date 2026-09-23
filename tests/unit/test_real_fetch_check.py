"""The hand-run fetch check (register item 89): it branches on the transport's
BackendStatusError, prints no response body and no foreign message, and names
leads by id, never by name."""

from __future__ import annotations

import pytest

from dodeal_ai.core.resilience import ExternalCallError
from dodeal_ai.tools.errors import BackendStatusError
from dodeal_ai.tools.leads import LeadsClient
from scripts import real_fetch_check
from tests.helpers.fake_leads import lead

SENTINEL = "SENTINEL-that-must-not-print"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "AUTH PROBLEM: backend returned 401"),
        (403, "AUTH PROBLEM: backend returned 403"),
        (422, "REQUEST REJECTED (422)"),
        (500, "BACKEND HTTP ERROR: 500"),
    ],
)
def test_a_refused_call_prints_its_status_and_no_body(capsys, status, expected) -> None:
    real_fetch_check._report_external_call_error(
        ExternalCallError("tool.get_leads", BackendStatusError(status))
    )
    err = capsys.readouterr().err
    assert expected in err
    assert "body" not in err.lower()


def test_a_network_failure_prints_the_type_and_never_the_message(capsys) -> None:
    real_fetch_check._report_external_call_error(
        ExternalCallError("tool.get_leads", ConnectionError(SENTINEL))
    )
    err = capsys.readouterr().err
    assert "ConnectionError" in err
    assert SENTINEL not in err


def test_success_prints_lead_ids_and_never_a_name(monkeypatch, capsys) -> None:
    """A dry run that answers: five ids at most, and no name."""
    monkeypatch.setenv("DODEAL_JWT_SIGNING_KEY", "test-key")
    leads = [lead(n, name=f"{SENTINEL}-{n}") for n in range(1, 8)]

    async def _answer(self, scope, **kwargs):
        return leads

    monkeypatch.setattr(LeadsClient, "get_leads", _answer)
    import asyncio

    assert asyncio.run(real_fetch_check._run("tenant-a", live=False)) == 0
    out = capsys.readouterr().out
    assert "First lead ids: 1, 2, 3, 4, 5" in out
    assert SENTINEL not in out
