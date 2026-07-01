"""Порты домена leagues: репозитории, шлюзы стендингов и резолв пользователей."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.modules.leagues.domain.entities import (
    Division,
    DivisionMembership,
    League,
    LeagueMembership,
)


@dataclass(frozen=True, slots=True)
class UserRef:
    id: uuid.UUID
    username: str
    display_name: str


@dataclass(frozen=True, slots=True)
class StandingRow:
    """Строка лидерборда лиги/дивизиона: пользователь + его рейтинговые метрики."""

    user_id: uuid.UUID
    username: str
    display_name: str
    skill_score: Decimal | None
    mean_brier: Decimal | None
    n_resolved: int
    rank: int  # позиция внутри выборки (1-based), присваивается use-case'ом


class LeagueRepository(Protocol):
    async def add(self, league: League) -> League: ...
    async def get_by_id(self, league_id: uuid.UUID) -> League | None: ...
    async def get_by_invite_code(self, code: str) -> League | None: ...


class LeagueMembershipRepository(Protocol):
    async def add(self, membership: LeagueMembership) -> LeagueMembership: ...
    async def remove(
        self, league_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool: ...
    async def is_member(
        self, league_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool: ...
    async def member_ids(self, league_id: uuid.UUID) -> list[uuid.UUID]: ...
    async def leagues_for_user(self, user_id: uuid.UUID) -> list[uuid.UUID]: ...
    async def count_members(self, league_id: uuid.UUID) -> int: ...


class DivisionRepository(Protocol):
    async def add(self, division: Division) -> Division: ...
    async def list_all(self) -> list[Division]: ...
    async def get_by_id(self, division_id: uuid.UUID) -> Division | None: ...
    async def get_by_level(self, level: int) -> Division | None: ...


class DivisionMembershipRepository(Protocol):
    async def get_for_user_season(
        self, user_id: uuid.UUID, season_id: uuid.UUID
    ) -> DivisionMembership | None: ...
    async def list_for_season_division(
        self, season_id: uuid.UUID, division_id: uuid.UUID
    ) -> list[DivisionMembership]: ...
    async def upsert(self, membership: DivisionMembership) -> None: ...


@runtime_checkable
class UserLookup(Protocol):
    async def resolve_username(self, username: str) -> UserRef | None: ...
    async def refs_by_ids(
        self, ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, UserRef]: ...


@runtime_checkable
class StandingsGateway(Protocol):
    """Метрики рейтинга пользователей из домена scoring (интеграционный шов)."""

    async def global_rows(
        self, user_ids: list[uuid.UUID]
    ) -> list[StandingRow]: ...

    async def season_ranked_ids(
        self, season_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]: ...

    async def season_rated_ids(self, season_id: uuid.UUID) -> list[uuid.UUID]:
        """Все пользователи с сезонным рейтингом (участники сезона)."""
        ...


@runtime_checkable
class InviteCodeGenerator(Protocol):
    def generate(self) -> str: ...
