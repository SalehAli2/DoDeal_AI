"""Unit A's HTTP surface: judge a note, resubmit a note, read the versions,
read a role brief.

TWO CHAINS (register item D1). The fetch routes depend on gate4_cost, the WHOLE
user chain -- Gate 1 (auth) -> Gate 2 (tenancy) -> Gate 4 (cost). The direct
routes depend on service_gate4_cost, the CRM's service chain, and a user token
is 401 there. /meta/versions takes either through gate4_either_principal. Gate
3 is parked; see core/auth/dependencies.py. One Depends, not a stack: the gates
chain through nested Depends, so listing them individually here would be a
second, silently divergent copy of the order.

The route hands the pipeline a TenantScope, never the RequestContext (design
note 0001, D1). Nothing below re-reads a tenant from anywhere.

NOTE TEXT IS NEVER ACCEPTED IN A BODY -- ON THE PRIMARY ROUTE. JudgementRequest
is two integers and extra="forbid": the note is already saved in the CRM and is
fetched by id (Design A). A caller posting `note` gets 422 invalid_request --
and, because core/errors.py replaces FastAPI's stock validation handler, that
422 does not quote their text back at them.

THE TWO DIRECT ROUTES ARE THE ONE EXCEPTION (DECISION[DIRECT_ROUTE]). They take
DirectJudgementRequest, which carries the saved note's text, because the CRM's
read surface has been unavailable and the CRM can send the note server-side
after the save. They sit behind the SERVICE chain (register item 92): the CRM
calls with its own token and names the author, and the scope is built with
`scope_for_author(request.author_id)`, so question caps and the per-user token
budget key on that author. `get_leads_client` is deliberately NOT in their
dependency chain: there is nothing to fetch, so no backend key is resolved and
no tool call can be made. The fetch routes remain the contract; see
ASSUMPTIONS.md, DECISION[DIRECT_ROUTE].

THE BRIEF ROUTE IS A GET AND ANSWERS 204 (register item 145), behind the
service chain only (register item 153). It is the one
route here that can succeed with no body: a brief every measure was suppressed
on is NOT SENT, because an empty daily email trains people to ignore the
channel. It reads the judgement store and the user directory, both of which are
fakes over invented files until the backend answers, and both of which are a
503 when nothing is configured -- never an empty brief.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request, Response
from fastapi.responses import JSONResponse

from dodeal_ai.core.auth.dependencies import (
    gate4_cost,
    gate4_either_principal,
    service_gate4_cost,
)
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import (
    BriefDeadlineExceeded,
    HistoryLoadShed,
    RepNumbersNotEnabled,
    SubjectNotFoundError,
)
from dodeal_ai.core.inflight import history_counter
from dodeal_ai.core.llm import LLMClient, get_llm_client
from dodeal_ai.tools.leads import LeadsClient, get_leads_client
from dodeal_ai.units.structured_intelligence.brief import (
    Brief,
    brief_window,
    build_brief,
)
from dodeal_ai.units.structured_intelligence.config import (
    TenantConfig,
    resolve_tenant_config,
)
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JudgementStore,
    get_judgement_store,
)
from dodeal_ai.units.structured_intelligence.measures import rolling_window
from dodeal_ai.units.structured_intelligence.pipeline import (
    PROMPT_SET_VERSION,
    RUBRIC_VERSION,
    JudgementDeps,
    ReplayedJudgement,
    judge_note,
    judge_note_direct,
    judge_note_history,
)
from dodeal_ai.units.structured_intelligence.schemas import (
    DirectJudgementRequest,
    HistoryJudgementRequest,
    Judgement,
    JudgementRequest,
    Versions,
)
from dodeal_ai.units.structured_intelligence.user_directory import (
    Role,
    User,
    UserDirectory,
    get_user_directory,
)

router = APIRouter(prefix="/api/v1", tags=["unit-a"])

_logger = logging.getLogger("dodeal_ai.unit_a")

# Sent, with "true", only on a judgement answered from the idempotency store.
REPLAY_HEADER = "Idempotent-Replay"


def _answer(judgement: Judgement, response: Response) -> Judgement:
    """The judgement as the route returns it, marked when it is a replay
    (register items 1 and 2)."""
    if isinstance(judgement, ReplayedJudgement):
        response.headers[REPLAY_HEADER] = "true"
    return judgement


def _deps(
    config: TenantConfig,
    leads: LeadsClient | None,
    llm: LLMClient,
    settings: Settings,
) -> JudgementDeps:
    """The pipeline's four seams for this request.

    `config` is the tenant's rules, resolved ONCE at the route's entry
    (register item 97) -- nothing below the route resolves them again, so one
    judgement can never be scored under two versions.

    `leads` is None on the direct routes: they do not fetch, so there is no
    client to hand them and saying so is more honest than passing one they must
    not call. See JudgementDeps.
    """
    return JudgementDeps(leads=leads, llm=llm, config=config, settings=settings)


@router.post("/notes/judgements")
async def create_judgement(
    request: JudgementRequest,
    response: Response,
    context: Annotated[RequestContext, Depends(gate4_cost)],
    leads: Annotated[LeadsClient, Depends(get_leads_client)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Judgement:
    """Judge one already-saved note."""
    judgement = await judge_note(
        context.scope(),
        request,
        resubmission=False,
        deps=_deps(await resolve_tenant_config(context.tenant), leads, llm, settings),
    )
    return _answer(judgement, response)


@router.post("/notes/judgements/resubmission")
async def create_resubmission_judgement(
    request: JudgementRequest,
    response: Response,
    context: Annotated[RequestContext, Depends(gate4_cost)],
    leads: Annotated[LeadsClient, Depends(get_leads_client)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Judgement:
    """Re-judge a note the salesperson has edited after a clarification prompt.

    Same body, same response shape. The difference is entirely in what happens
    to the clarification prompt: none is ever sent, and attempts are read but
    never incremented -- we asked once, they answered, and asking again about
    their answer is how a helpful prompt becomes nagging.

    An edited note has a new fingerprint, so it is a NEW judgement rather than
    a 409: the idempotency key is per note text, not per note id.
    """
    judgement = await judge_note(
        context.scope(),
        request,
        resubmission=True,
        deps=_deps(await resolve_tenant_config(context.tenant), leads, llm, settings),
    )
    return _answer(judgement, response)


@router.post("/notes/judgements/direct")
async def create_direct_judgement(
    request: DirectJudgementRequest,
    response: Response,
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Judgement:
    """Judge a note the CRM has just saved and sent us. DECISION[DIRECT_ROUTE].

    The service chain, the same response, and the same pipeline from the
    length check down. The body carries the saved note's text, its three ids
    and the four lead fields the classifier reads; nothing else is accepted
    (extra="forbid"), and note_text over 4,000 characters is a 422 before the
    pipeline is entered. The scope names the body's author (register item 92).

    No LeadsClient in the signature, deliberately -- there is nothing to fetch.
    """
    judgement = await judge_note_direct(
        context.scope_for_author(request.author_id),
        request,
        resubmission=False,
        deps=_deps(await resolve_tenant_config(context.tenant), None, llm, settings),
    )
    return _answer(judgement, response)


@router.post("/notes/judgements/direct/resubmission")
async def create_direct_resubmission_judgement(
    request: DirectJudgementRequest,
    response: Response,
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Judgement:
    """The direct route's resubmission variant.

    `resubmission` means here exactly what it means on the fetch route: no
    clarification prompt is ever sent, attempts are read and never incremented,
    and `decision.original_note_fingerprint` carries the note we first prompted
    on. An edited note is a new fingerprint and so a new judgement, not a 409 --
    which on this route is the ordinary case, because the CRM sends the edited
    text itself.
    """
    judgement = await judge_note_direct(
        context.scope_for_author(request.author_id),
        request,
        resubmission=True,
        deps=_deps(await resolve_tenant_config(context.tenant), None, llm, settings),
    )
    return _answer(judgement, response)


async def _history_slot(request: Request) -> AsyncIterator[None]:
    """The history bulkhead (register item 127): at most `history_max_inflight`
    history judgements at once in this process, INSIDE the global cap.

    Over it, 503 history_load_shed with Retry-After: 1, before anything is
    reserved or spent. The slot is given back in a `finally`, so a history
    judgement that fails cannot leak one.
    """
    if not history_counter.acquire(get_settings().history_max_inflight):
        _logger.warning(
            "history_load_shed",
            extra={
                "reason_code": "history_load_shed",
                "request_id": getattr(request.state, "request_id", "unknown"),
                "inflight": history_counter.count,
            },
        )
        raise HistoryLoadShed()
    try:
        yield
    finally:
        history_counter.release()


@router.post("/notes/judgements/history")
async def create_history_judgement(
    request: HistoryJudgementRequest,
    response: Response,
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    _slot: Annotated[None, Depends(_history_slot)],
    llm: Annotated[LLMClient, Depends(get_llm_client)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Judgement:
    """Judge an OLD saved note so the measures have a past (register item 127).

    The service chain, the direct body plus `note_created_at` (an aware time;
    naive is 422), and the direct route's pipeline with no question ever sent,
    no db2 counter touched and the tokens charged to the tenant's HISTORY
    budget alone -- so a backfill can neither pester anyone nor spend the live
    budget. The history bulkhead bounds how many run at once.
    """
    judgement = await judge_note_history(
        context.scope_for_author(request.author_id, budget="history"),
        request,
        deps=_deps(await resolve_tenant_config(context.tenant), None, llm, settings),
    )
    return _answer(judgement, response)


@router.get("/meta/versions")
async def read_versions(
    context: Annotated[RequestContext, Depends(gate4_either_principal)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Versions:
    """The version strings a judgement is stamped with, policy_version included.

    CAREFUL, one field means something slightly different here than on a
    judgement. On a judgement, `model_version` is what the provider REPORTED it
    ran. Here there is no call and so no report, so this is the CONFIGURED pin
    (DODEAL_LLM_MODEL) -- what WOULD run. It is "" until a model is configured,
    which is honest: no model is pinned yet.

    Behind the gates like the others, so version strings are not a public
    fingerprint of the deployment -- either principal's, since the CRM reads
    them with its service token and a person's client with theirs.
    """
    config = await resolve_tenant_config(context.tenant)
    return Versions(
        rubric_version=RUBRIC_VERSION,
        prompt_version=PROMPT_SET_VERSION,
        model_version=settings.llm_model,
        config_version=config.config_version,
        policy_version=config.policy_version,
    )


def _subject(users: list[User], subject_id: int) -> User:
    """The person this brief is about, or a 404.

    NOT a 204. "There is no such person" and "nothing to report about this
    person" are different answers, and a CRM sending a wrong id would read a
    204 as a quiet day, every day, for ever.
    """
    for user in users:
        if user.user_id == subject_id:
            return user
    raise SubjectNotFoundError()


async def rep_numbers_config(
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
) -> TenantConfig:
    """The tenant's rules for a route that shows a person's figures, or 403
    rep_numbers_not_enabled (register item 154). Declared before the store in
    each route, so a tenant that has not opted in is told so first."""
    config = await resolve_tenant_config(context.tenant)
    if not config.rep_numbers_enabled:
        raise RepNumbersNotEnabled()
    return config


def deadline_exceeded(context: RequestContext) -> BriefDeadlineExceeded:
    """Log the brief or measure that ran out of time; hand back the error."""
    _logger.warning(
        "brief_deadline_exceeded",
        extra={
            "reason_code": "brief_deadline_exceeded",
            "tenant": context.tenant,
            "request_id": context.request_id,
        },
    )
    return BriefDeadlineExceeded()


@router.get(
    "/briefs/{role}/{subject_id}",
    responses={204: {"description": "Nothing to report; no brief is sent."}},
)
async def read_brief(
    role: Role,
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    config: Annotated[TenantConfig, Depends(rep_numbers_config)],
    store: Annotated[JudgementStore, Depends(get_judgement_store)],
    directory: Annotated[UserDirectory, Depends(get_user_directory)],
    settings: Annotated[Settings, Depends(get_settings)],
    subject_id: Annotated[int, Path(ge=1)],
) -> Response:
    """One role brief, on demand. Register item 145.

    NO SCHEDULER. The CRM calls at 07:30; whether this service needs a clock of
    its own is an open question, and a scheduler built before the answer would
    be a second thing sending briefs.

    204 WHEN THERE IS NOTHING TO SAY, and it is the rule the piece is for: an
    empty daily email trains people to ignore the channel within a fortnight.
    A 204 means every measure was suppressed, not that the request was wrong --
    an unknown subject is a 404 and an unwired store is a 503.

    `role` is what the CALLER asked for, not what the subject's title entitles
    them to: a head of sales may want the rep view of one of their people.

    JSON (register item 154): {text, period, lines, flagged_note_ids}, under
    the judgement deadline with its own 503 brief_deadline_exceeded, and 403
    rep_numbers_not_enabled for a tenant that has not opted in.

    THE SERVICE CHAIN ONLY (register item 153). A person's token is 401 here:
    the CRM asks for a brief with its own token and decides, on its side, who
    may read whose. The chain still holds the tenant -- Host match, tenant cost
    counter -- so a brief never crosses one.
    """
    try:
        async with asyncio.timeout(settings.judgement_deadline_seconds):
            brief = await _brief(role, subject_id, context, store, directory, config)
    except TimeoutError:
        raise deadline_exceeded(context) from None
    if brief is None:
        return Response(status_code=204)
    return JSONResponse(brief.body())


async def _brief(
    role: Role,
    subject_id: int,
    context: RequestContext,
    store: JudgementStore,
    directory: UserDirectory,
    config: TenantConfig,
) -> Brief | None:
    """The brief's reads and its arithmetic, under the route's deadline. The
    store read reaches back far enough for the two weeks the sections compare
    (register item 145); the rolling measures still read their own window."""
    now = datetime.now(UTC)
    since, until = rolling_window(now, config)
    read_since, _ = brief_window(now, config)

    users = list(await directory.users(context.tenant))
    subject = _subject(users, subject_id)

    # The rep brief narrows the store read by author; the other two need the
    # whole tenant's window, because they roll up across people and teams.
    rows = await store.rows_between(
        context.tenant,
        since=read_since,
        until=until,
        author_id=subject.user_id if role is Role.REP else None,
    )

    return build_brief(
        role,
        subject,
        users=users,
        rows=rows,
        since=since,
        until=until,
        config=config,
    )
