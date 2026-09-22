"""The per-rep and per-team measures, as data (register item 144).

  GET /api/v1/measures/reps/{author_id}   one person's three measures
  GET /api/v1/measures/teams/{team}       the team pooled, then each member

Behind the service chain, the rep-numbers switch (403 when the tenant has not
opted in) and the judgement deadline (503 brief_deadline_exceeded), over the
same store and directory as the brief. Each measure is value, state, n, floor
and excluded: a measure under the evidence floor is a STATE with a null value,
never a 0. No note text and no note id reaches either answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from dodeal_ai.api.routes.judgements import deadline_exceeded, rep_numbers_config
from dodeal_ai.core.auth.dependencies import service_gate4_cost
from dodeal_ai.core.config import Settings, get_settings
from dodeal_ai.core.context import RequestContext
from dodeal_ai.core.errors import SubjectNotFoundError
from dodeal_ai.units.structured_intelligence.config import TenantConfig
from dodeal_ai.units.structured_intelligence.judgement_rows import (
    JudgementRow,
    JudgementStore,
    get_judgement_store,
)
from dodeal_ai.units.structured_intelligence.measures import (
    figures,
    local_dates,
    rolling_window,
)
from dodeal_ai.units.structured_intelligence.user_directory import (
    User,
    UserDirectory,
    get_user_directory,
)

router = APIRouter(prefix="/api/v1/measures", tags=["unit-a"])


def _window(since: datetime, until: datetime, config: TenantConfig) -> dict:
    """The window as UTC instants and as the tenant's local first and last day."""
    first, last = local_dates(since, until, config)
    return {
        "since": since.isoformat(),
        "until": until.isoformat(),
        "first_day": first.isoformat(),
        "last_day": last.isoformat(),
        "days": config.rolling_window_days,
    }


def _theirs(rows: Sequence[JudgementRow], author_id: int) -> list[JudgementRow]:
    return [row for row in rows if row.author_id == author_id]


@router.get("/reps/{author_id}")
async def read_rep_measures(
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    config: Annotated[TenantConfig, Depends(rep_numbers_config)],
    store: Annotated[JudgementStore, Depends(get_judgement_store)],
    directory: Annotated[UserDirectory, Depends(get_user_directory)],
    settings: Annotated[Settings, Depends(get_settings)],
    author_id: Annotated[int, Path(ge=1)],
) -> dict:
    """One person's three measures, their average total and the window. An
    author the directory does not list is 404, not an empty answer."""
    try:
        async with asyncio.timeout(settings.judgement_deadline_seconds):
            since, until = rolling_window(datetime.now(UTC), config)
            person = _person(await directory.users(context.tenant), author_id)
            rows = await store.rows_between(
                context.tenant, since=since, until=until, author_id=author_id
            )
    except TimeoutError:
        raise deadline_exceeded(context) from None
    return {
        "author_id": person.user_id,
        "name": person.name,
        **figures(rows, config),
        "window": _window(since, until, config),
    }


@router.get("/teams/{team}")
async def read_team_measures(
    context: Annotated[RequestContext, Depends(service_gate4_cost)],
    config: Annotated[TenantConfig, Depends(rep_numbers_config)],
    store: Annotated[JudgementStore, Depends(get_judgement_store)],
    directory: Annotated[UserDirectory, Depends(get_user_directory)],
    settings: Annotated[Settings, Depends(get_settings)],
    team: Annotated[str, Path(min_length=1, max_length=100)],
) -> dict:
    """The team's measures over its members' rows POOLED, then each member's.
    A team nobody in the directory belongs to is 404."""
    try:
        async with asyncio.timeout(settings.judgement_deadline_seconds):
            since, until = rolling_window(datetime.now(UTC), config)
            members = sorted(
                (
                    user
                    for user in await directory.users(context.tenant)
                    if user.team == team
                ),
                key=lambda user: user.name,
            )
            if not members:
                raise SubjectNotFoundError()
            rows = await store.rows_between(context.tenant, since=since, until=until)
    except TimeoutError:
        raise deadline_exceeded(context) from None
    member_ids = {user.user_id for user in members}
    team_rows = [row for row in rows if row.author_id in member_ids]
    return {
        "team": team,
        **figures(team_rows, config),
        "window": _window(since, until, config),
        "members": [
            {
                "author_id": user.user_id,
                "name": user.name,
                **figures(_theirs(team_rows, user.user_id), config),
            }
            for user in members
        ],
    }


def _person(users: Sequence[User], author_id: int) -> User:
    for user in users:
        if user.user_id == author_id:
            return user
    raise SubjectNotFoundError()
