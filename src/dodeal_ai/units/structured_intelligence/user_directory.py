"""Who the people are — the second backend ask, and the fake that stands in
for it.

A BRIEF NEEDS NAMES, NOT IDS. Everything this service holds about a person is
`author_id`, an integer the CRM chose. "Agent 3312 is flagged on 40% of their
notes" is not something a head of sales can act on, and a brief that printed it
would be forwarded to someone who had to go and look the number up. So the
directory is a seam, exactly as the judgement store is, and for the same
reason: it is the CRM's data, we do not hold it, and the real implementation is
a session of its own when the backend answers.

WHAT THE REAL ONE NEEDS, as the ask: for one tenant, every user who writes or
reads notes, each with

    user_id   the SAME id space as a note's `author_id`. This is the whole
              point of the ask and the one field that must not be approximated:
              if the directory keys on the CRM's login id instead, every brief
              names the wrong person, and nothing in a brief would look wrong.
    name      what to call them, as they are called in the CRM.
    role      rep, team leader, or head of sales. Ours, not theirs: whatever
              their nine role names turn out to be (Q4), they map onto these
              three, because these three are the briefs that exist.
    team      which team they are in. A rep and a team leader have one; a head
              of sales spans them all and has none.

ONE READ, as the store has one. `users(tenant)` returns the lot, and the briefs
filter in code. That is the smallest thing to ask a backend engineer to build,
and the head-of-sales brief needs every user anyway. If the real directory
turns out to be large enough that this is wasteful, the ask grows a filter --
a decision for when it exists, not a shape to guess at now.

REAL NAMES NEVER ENTER THIS REPO. The fake reads a JSON file whose path comes
from an environment variable and refuses a path inside the repository, the same
guard the scored set and the judgement rows have. This is the strongest case of
the three: the file is a staff list.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from enum import StrEnum
from functools import cache
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dodeal_ai.core.errors import BriefStoreUnavailable
from dodeal_ai.units.structured_intelligence.eval_set import REPO_ROOT

# Where the invented staff list lives, given at runtime. An ENVIRONMENT
# VARIABLE and not a Settings field, for the same reason the judgement rows'
# path is one: a Settings field would reach `.env.example` and be copied into a
# deployment. Unset means there is no directory.
USER_DIRECTORY_PATH_ENV = "DODEAL_USER_DIRECTORY_PATH"


class UserDirectoryError(Exception):
    """The directory could not be read, or an entry in it is not a user.

    Names the tenant key and the entry's position, and NEVER a field value. An
    entry here is a real person's name by the time this matters, and an error
    message is the easiest place in a program for one to reach a terminal.
    """


class Role(StrEnum):
    """The three briefs that exist, which is why there are three roles.

    Deliberately OURS and not the CRM's. Product has not confirmed the real
    role names (Q4, ~9 of them heard), and inventing them here would be
    inventing a backend field. Whatever they turn out to be, the ask is that
    they map onto these three -- because a fourth role would need a fourth
    brief, and there is no fourth brief.
    """

    REP = "rep"
    TEAM_LEADER = "team_leader"
    HEAD_OF_SALES = "head_of_sales"


class User(BaseModel):
    """One person, as a brief needs them.

    Four fields and no more. There is no email, no phone and no manager id:
    this service sends nothing to anybody -- the CRM does -- so a contact
    detail here would be personal data held for no purpose at all.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    # ge=1 and the same id space as JudgementRow.author_id. A directory keyed
    # on anything else names the wrong person in every brief, silently.
    user_id: int = Field(ge=1)
    name: str = Field(min_length=1)
    role: Role
    team: str | None = None

    @model_validator(mode="after")
    def _team_matches_role(self) -> User:
        """A rep and a team leader are in a team; a head of sales spans them.

        Refused rather than tolerated because both mistakes are silent: a rep
        with no team appears in nobody's team brief, and a head of sales with
        one appears in a team brief as though they were a member of it.
        """
        if self.role is Role.HEAD_OF_SALES:
            if self.team is not None:
                raise ValueError("a head of sales has no team")
        elif self.team is None:
            raise ValueError("a rep and a team leader need a team")
        return self


@runtime_checkable
class UserDirectory(Protocol):
    """The one read a brief needs: this tenant's people.

    Async because the real implementation is an HTTP call to the CRM. The fake
    answers from memory and is still async, so swapping it changes no caller.

    An implementation owes ONE thing beyond the fields: a user_id in the SAME
    space as a note's author_id. Nothing downstream can detect a directory that
    keyed on something else -- every brief would simply name the wrong people.
    """

    async def users(self, tenant: str) -> Sequence[User]:
        """Every user this tenant has, in any order. Empty means the tenant has
        nobody, which is a state and not an error."""
        ...


def configured_path() -> Path | None:
    """The directory file's path from the environment, or None when unset.

    None is the ordinary case: there is no directory until the backend answers,
    and CI never sets this. Blank is treated as unset.
    """
    raw = os.environ.get(USER_DIRECTORY_PATH_ENV, "").strip()
    return Path(raw) if raw else None


class FileUserDirectory:
    """A UserDirectory over invented people in a JSON file. A FAKE, named one.

        {"tenant-a": [ {user}, {user} ], "tenant-b": [ ... ]}

    The tenant is the container and not a field, exactly as in the judgement
    rows: each tenant is its own database in the CRM, so a `tenant` column on a
    person would be a field the backend does not have.

    Read once at construction, so no request reads disk.
    """

    def __init__(self, users_by_tenant: dict[str, list[User]]) -> None:
        self._users = users_by_tenant

    @classmethod
    def from_path(cls, path: Path) -> FileUserDirectory:
        """Load and validate every entry in `path`.

        Refuses a path inside the repository first of all. A real directory is
        a staff list, and the easiest way for one to be committed is a loader
        that accepted a path which made it convenient.
        """
        resolved = path.expanduser().resolve()
        if resolved.is_relative_to(REPO_ROOT):
            raise UserDirectoryError(
                f"refusing a user directory inside the repository: {resolved}. "
                "It is a staff list -- keep it out of the repo."
            )
        try:
            raw = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            raise UserDirectoryError(
                f"cannot read {resolved}: {type(exc).__name__}"
            ) from None
        try:
            document = json.loads(raw)
        except ValueError:
            raise UserDirectoryError(f"{resolved.name}: not one JSON object") from None
        if not isinstance(document, dict):
            raise UserDirectoryError(f"{resolved.name}: not one JSON object")

        users_by_tenant: dict[str, list[User]] = {}
        for tenant, entries in document.items():
            if not isinstance(entries, list):
                raise UserDirectoryError(f"{resolved.name}: {tenant} is not a list")
            users_by_tenant[tenant] = [
                cls._user(resolved.name, tenant, position, entry)
                for position, entry in enumerate(entries, start=1)
            ]
        return cls(users_by_tenant)

    @staticmethod
    def _user(name: str, tenant: str, position: int, entry: object) -> User:
        """One entry as a user, or a refusal naming where it is and what kind
        of thing went wrong -- never what the entry said."""
        try:
            return User.model_validate(entry)
        except ValueError as exc:
            raise UserDirectoryError(
                f"{name}: {tenant} entry {position}: {type(exc).__name__}"
            ) from None

    async def users(self, tenant: str) -> Sequence[User]:
        """This tenant's people, in file order. A tenant nobody has listed
        answers empty; that is a state, and the route turns it into the one
        refusal a caller can act on."""
        return list(self._users.get(tenant, ()))


@cache
def get_user_directory() -> UserDirectory:
    """The directory this deployment has, for the routes that need names.

    Cached for the same reason the store's provider is, and unset is the same
    503: a brief that fell back to printing ids would be forwarded to somebody
    who had to go and look every one of them up.
    """
    path = configured_path()
    if path is None:
        raise BriefStoreUnavailable()
    return FileUserDirectory.from_path(path)
