"""Load scenario 5: the same burst on two tenants at once.

Ten judgements on tenant-a and ten on tenant-c share one gather with five requests
that carry a tenant-a token to the tenant-c Host. The leads override serves each
request the corpus of the tenant its verified context names. Tenant-c's corpus is
tenant-a's with every id moved up by 100000 (make_tenant_c_fixture.py), so the
two share no id and a key or a judgement can be traced to its tenant by id alone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Annotated

import httpx
import pytest
from fastapi import Depends
from redis import asyncio as redis_async

from dodeal_ai.core.auth.dependencies import gate4_cost
from dodeal_ai.core.context import RequestContext
from dodeal_ai.main import app
from dodeal_ai.tools.leads import get_leads_client
from tests.helpers import tokens
from tests.helpers.fake_leads import FakeLeadsClient, load_fixture_client
from tests.load.conftest import (
    BASE_DOMAIN,
    JUDGE,
    SUBJECT,
    TENANT,
    notes_passing_gate,
)
from tests.load.make_tenant_c_fixture import SOURCE, TARGET, shifted
from tests.load.make_tenant_c_fixture import TENANT as TENANT_C

BURST = 10
CROSSED = 5
# One subject per tenant, each inside its own tenant's id space.
SUBJECTS = {TENANT: SUBJECT, TENANT_C: SUBJECT + 100000}
OTHER = {TENANT: TENANT_C, TENANT_C: TENANT}


def _headers(token_tenant: str, host_tenant: str) -> dict[str, str]:
    token = tokens.mint_token(
        subdomain=token_tenant,
        sub=SUBJECTS[token_tenant],
        database=f"crm_{token_tenant.replace('-', '_')}",
    )
    return {
        "Authorization": f"Bearer {token}",
        "Host": f"{host_tenant}.{BASE_DOMAIN}",
    }


def _leads_for(
    corpora: dict[str, FakeLeadsClient],
) -> Callable[[RequestContext], FakeLeadsClient]:
    """The leads override: the corpus of the tenant the gates verified."""

    def leads(
        context: Annotated[RequestContext, Depends(gate4_cost)],
    ) -> FakeLeadsClient:
        return corpora[context.tenant]

    return leads


def _corpus_ids(payload: dict) -> set[str]:
    """Every lead, note and author id in a raw corpus, as the strings keys hold."""
    ids = {str(lead["id"]) for lead in payload["leads"]}
    for section in ("notes", "timeline_events"):
        for lead_id, records in payload[section].items():
            ids.add(lead_id)
            ids.update(str(raw["id"]) for raw in records)
            ids.update(str(raw["author_id"]) for raw in records)
    return ids


def _parse_key(key: str) -> tuple[str, str, frozenset[str]]:
    """(namespace, tenant, ids) for a key the app writes. The fingerprint in an
    idempotency key is not an id and is dropped, so it never reaches a report."""
    namespace, *rest = key.split(":")
    if namespace in ("cost", "tokens"):
        _counter, tenant, *ids = rest
    elif namespace == "idem":
        tenant, _operation, note_id, _fingerprint = rest
        ids = [note_id]
    elif namespace in ("ratelimit", "attempt", "attempt_fp"):
        tenant, *ids = rest
    else:
        pytest.fail(f"a key in a namespace this test does not know: {namespace}")
    return namespace, tenant, frozenset(ids)


async def _parsed_keys(store: redis_async.Redis) -> list[tuple[str, str, frozenset]]:
    return [_parse_key(key) async for key in store.scan_iter()]


def test_the_tenant_c_corpus_is_the_generator_output_and_shares_no_id_with_tenant_a():
    """The committed tenant-c corpus is the generator's output from tenant-a, and no id appears in both."""
    source = json.loads(SOURCE.read_text(encoding="utf-8"))
    committed = json.loads(TARGET.read_text(encoding="utf-8"))

    assert committed == shifted(source)
    assert _corpus_ids(source).isdisjoint(_corpus_ids(committed))


async def test_s5_two_tenants_in_one_burst_are_judged_and_keyed_apart(lane):
    """Ten judgements per tenant at once each come from their own corpus and write only their own tenant's keys, and five crossed requests are all 403."""
    corpora = {TENANT: lane.leads, TENANT_C: load_fixture_client(TARGET)}
    app.dependency_overrides[get_leads_client] = _leads_for(corpora)
    picked = {
        tenant: notes_passing_gate(leads, tenant, one_per_lead=True)[:BURST]
        for tenant, leads in corpora.items()
    }
    assert [len(notes) for notes in picked.values()] == [BURST, BURST]
    lane.script(judgements=2 * BURST)

    sent = [
        (tenant, lead_id, note.id)
        for tenant in picked
        for lead_id, note in picked[tenant]
    ]
    crossed = [(lead_id, note.id) for lead_id, note in picked[TENANT_C][:CROSSED]]
    outcomes = await asyncio.gather(
        *(
            lane.client.post(
                JUDGE,
                json={"lead_id": lead_id, "note_id": note_id},
                headers=_headers(tenant, tenant),
            )
            for tenant, lead_id, note_id in sent
        ),
        *(
            lane.client.post(
                JUDGE,
                json={"lead_id": lead_id, "note_id": note_id},
                headers=_headers(TENANT, TENANT_C),
            )
            for lead_id, note_id in crossed
        ),
        return_exceptions=True,
    )
    judged, refused = outcomes[: len(sent)], outcomes[len(sent) :]

    served: dict[str, list[tuple[int, int, int]]] = {TENANT: [], TENANT_C: []}
    for (tenant, _, _), outcome in zip(sent, judged, strict=True):
        if isinstance(outcome, httpx.Response) and outcome.status_code == 200:
            body = outcome.json()
            served[tenant].append((body["lead_id"], body["note_id"], body["author_id"]))
    assert served == {
        tenant: [(lead_id, note.id, note.author_id) for lead_id, note in notes]
        for tenant, notes in picked.items()
    }

    own_ids = {
        tenant: _corpus_ids(json.loads(path.read_text(encoding="utf-8")))
        | {str(SUBJECTS[tenant])}
        for tenant, path in ((TENANT, SOURCE), (TENANT_C, TARGET))
    }
    keys = {
        10: await _parsed_keys(lane.stores.cost),
        11: await _parsed_keys(lane.stores.operational),
    }
    assert {db: {tenant for _, tenant, _ in parsed} for db, parsed in keys.items()} == {
        10: {TENANT, TENANT_C},
        11: {TENANT, TENANT_C},
    }
    assert [
        (db, namespace, tenant, sorted(ids))
        for db, parsed in keys.items()
        for namespace, tenant, ids in parsed
        if not ids <= own_ids[tenant] or ids & own_ids[OTHER[tenant]]
    ] == []
    # Charged once per judgement: the override's gate is the route's, and a
    # crossed request is refused before the cost gate counts it.
    assert [
        await lane.stores.cost.get(f"cost:user:{tenant}:{SUBJECTS[tenant]}")
        for tenant in (TENANT, TENANT_C)
    ] == [str(BURST), str(BURST)]

    assert [
        o.status_code if isinstance(o, httpx.Response) else type(o).__name__
        for o in refused
    ] == [403] * CROSSED
