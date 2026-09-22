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

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response
from fastapi.responses import PlainTextResponse

from dodeal_ai.core.auth.dependencies import (
    gate4_cost,
    gate4_either_principal,
    service_gate4_cost,
)
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import SubjectNotFoundError
from dodeal_ai.core.llm import LLMClient, get_llm_client
from dodeal_ai.tools.leads import LeadsClient, get_leads_client
from dodeal_ai.units.structured_intelligence.brief import build_brief
from dodeal_ai.units.structured_intelligence.config import get_tenant_config
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
)
from dodeal_ai.units.structured_intelligence.schemas import (
    DirectJudgementRequest,
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

# Sent, with "true", only on a judgement answered from the idempotency store.
REPLAY_HEADER = "Idempotent-Replay"


def _answer(judgement: Judgement, response: Response) -> Judgement:
    """The judgement as the route returns it, marked when it is a replay
    (register items 1 and 2)."""
    if isinstance(judgement, ReplayedJudgement):
        response.headers[REPLAY_HEADER] = "true"
    return judgement


def _deps(
    context: RequestContext,
    leads: LeadsClient | None,
    llm: LLMClient,
    settings: Settings,
) -> JudgementDeps:
    """The pipeline's four seams for this request.

    `leads` is None on the direct routes: they do not fetch, so there is no
    client to hand them and saying so is more honest than passing one they must
    not call. See JudgementDeps.
    """
    return JudgementDeps(
        leads=leads,
        llm=llm,
        config=get_tenant_config(context.tenant),
        settings=settings,
    )


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
        deps=_deps(context, leads, llm, settings),
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
        deps=_deps(context, leads, llm, settings),
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
        deps=_deps(context, None, llm, settings),
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
        deps=_deps(context, None, llm, settings),
    )
    return _answer(judgement, response)


@router.get("/meta/versions")
async def read_versions(
    context: Annotated[RequestContext, Depends(gate4_either_principal)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> Versions:
    """The four version strings a judgement is stamped with.

    CAREFUL, one field means something slightly different here than on a
    judgement. On a judgement, `model_version` is what the provider REPORTED it
    ran. Here there is no call and so no report, so this is the CONFIGURED pin
    (DODEAL_LLM_MODEL) -- what WOULD run. It is "" until a model is configured,
    which is honest: no model is pinned yet.

    Behind the gates like the others, so version strings are not a public
    fingerprint of the deployment -- either principal's, since the CRM reads
    them with its service token and a person's client with theirs.
    """
    return Versions(
        rubric_version=RUBRIC_VERSION,
        prompt_version=PROMPT_SET_VERSION,
        model_version=settings.llm_model,
        config_version=get_tenant_config(context.tenant).config_version,
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


@router.get(
    "/briefs/{role}/{subject_id}",
    response_class=PlainTextResponse,
    responses={204: {"description": "Nothing to report; no brief is sent."}},
)
async def read_brief(
    role: Role,
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    store: Annotated[JudgementStore, Depends(get_judgement_store)],
    directory: Annotated[UserDirectory, Depends(get_user_directory)],
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

    THE SERVICE CHAIN ONLY (register item 153). A person's token is 401 here:
    the CRM asks for a brief with its own token and decides, on its side, who
    may read whose. The chain still holds the tenant -- Host match, tenant cost
    counter -- so a brief never crosses one.
    """
    config = get_tenant_config(context.tenant)
    since, until = rolling_window(datetime.now(UTC), config)

    users = list(await directory.users(context.tenant))
    subject = _subject(users, subject_id)

    # The rep brief narrows the store read by author; the other two need the
    # whole tenant's window, because they roll up across people and teams.
    rows = await store.rows_between(
        context.tenant,
        since=since,
        until=until,
        author_id=subject.user_id if role is Role.REP else None,
    )

    text = build_brief(
        role,
        subject,
        users=users,
        rows=rows,
        since=since,
        until=until,
        config=config,
    )
    if text is None:
        return Response(status_code=204)
    return PlainTextResponse(text)
