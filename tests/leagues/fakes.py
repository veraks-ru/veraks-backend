"""In-memory фейки портов домена leagues для юнит-тестов use-cases."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from app.modules.leagues.domain.entities import (
    Division,
    DivisionMembership,
    League,
    LeagueMembership,
)
from app.modules.leagues.domain.errors import LeagueNotFoundError
from app.modules.leagues.ports.repositories import StandingRow, UserRef


class InMemoryLeagueRepository:
    """Хранилище лиг; ``list_all`` отдаёт новые первыми, как SQL-адаптер."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, League] = {}
        self.deleted: list[uuid.UUID] = []

    def seed(self, league: League) -> League:
        self._by_id[league.id] = league
        return league

    async def add(self, league: League) -> League:
        self._by_id[league.id] = league
        return league

    async def get_by_id(self, league_id: uuid.UUID) -> League | None:
        return self._by_id.get(league_id)

    async def get_by_invite_code(self, code: str) -> League | None:
        for league in self._by_id.values():
            if league.invite_code == code:
                return league
        return None

    async def list_all(self, *, limit: int, offset: int) -> list[League]:
        ordered = sorted(
            self._by_id.values(), key=lambda x: x.created_at, reverse=True
        )
        return ordered[offset : offset + limit]

    async def count_all(self) -> int:
        return len(self._by_id)

    async def rename(self, league_id: uuid.UUID, name: str) -> League:
        league = self._by_id.get(league_id)
        if league is None:
            raise LeagueNotFoundError("Лига не найдена")
        league.name = name
        return league

    async def delete(self, league_id: uuid.UUID) -> bool:
        if league_id not in self._by_id:
            return False
        del self._by_id[league_id]
        self.deleted.append(league_id)
        return True


class InMemoryLeagueMembershipRepository:
    def __init__(self) -> None:
        self._rows: list[LeagueMembership] = []

    async def add(self, membership: LeagueMembership) -> LeagueMembership:
        self._rows.append(membership)
        return membership

    async def remove(self, league_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        before = len(self._rows)
        self._rows = [
            r
            for r in self._rows
            if not (r.league_id == league_id and r.user_id == user_id)
        ]
        return len(self._rows) < before

    async def is_member(self, league_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        return any(
            r.league_id == league_id and r.user_id == user_id for r in self._rows
        )

    async def member_ids(self, league_id: uuid.UUID) -> list[uuid.UUID]:
        return [r.user_id for r in self._rows if r.league_id == league_id]

    async def leagues_for_user(self, user_id: uuid.UUID) -> list[uuid.UUID]:
        return [r.league_id for r in self._rows if r.user_id == user_id]

    async def count_members(self, league_id: uuid.UUID) -> int:
        return len(await self.member_ids(league_id))


class InMemoryDivisionRepository:
    def __init__(self, levels: int = 3) -> None:
        self._divisions = [
            Division(level=lvl, title=f"Дивизион {lvl}") for lvl in range(1, levels + 1)
        ]

    async def add(self, division: Division) -> Division:
        self._divisions.append(division)
        return division

    async def list_all(self) -> list[Division]:
        return list(self._divisions)

    async def get_by_id(self, division_id: uuid.UUID) -> Division | None:
        return next((d for d in self._divisions if d.id == division_id), None)

    async def get_by_level(self, level: int) -> Division | None:
        return next((d for d in self._divisions if d.level == level), None)


class InMemoryDivisionMembershipRepository:
    def __init__(self) -> None:
        self.rows: dict[tuple[uuid.UUID, uuid.UUID], DivisionMembership] = {}

    async def get_for_user_season(
        self, user_id: uuid.UUID, season_id: uuid.UUID
    ) -> DivisionMembership | None:
        return self.rows.get((user_id, season_id))

    async def list_for_season_division(
        self, season_id: uuid.UUID, division_id: uuid.UUID
    ) -> list[DivisionMembership]:
        return [
            m
            for m in self.rows.values()
            if m.season_id == season_id and m.division_id == division_id
        ]

    async def upsert(self, membership: DivisionMembership) -> None:
        self.rows[(membership.user_id, membership.season_id)] = membership


class FakeStandingsGateway:
    """Глобальные метрики по словарю ``user_id → skill_score`` (None — без рейтинга)."""

    def __init__(self, skills: dict[uuid.UUID, Decimal | None] | None = None) -> None:
        self._skills = skills or {}

    async def global_rows(self, user_ids: list[uuid.UUID]) -> list[StandingRow]:
        return [
            StandingRow(
                user_id=uid,
                username=f"u{str(uid)[:4]}",
                display_name="Участник",
                skill_score=self._skills.get(uid),
                mean_brier=None,
                n_resolved=0,
                rank=0,
            )
            for uid in user_ids
        ]

    async def season_ranked_ids(
        self, season_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        return sorted(
            user_ids,
            key=lambda uid: (
                self._skills.get(uid) is None,
                -(self._skills.get(uid) or Decimal(0)),
            ),
        )

    async def season_rated_ids(self, season_id: uuid.UUID) -> list[uuid.UUID]:
        return list(self._skills)


class FakeUserLookup:
    """Справочник активных аккаунтов (для первичного посева дивизионов)."""

    def __init__(self, user_ids: list[uuid.UUID] | None = None) -> None:
        self._ids = user_ids or []

    async def resolve_username(self, username: str) -> UserRef | None:
        return None

    async def refs_by_ids(self, ids: list[uuid.UUID]) -> dict[uuid.UUID, UserRef]:
        return {
            uid: UserRef(id=uid, username=f"u{str(uid)[:4]}", display_name="Участник")
            for uid in ids
        }

    async def active_user_ids(self) -> list[uuid.UUID]:
        return list(self._ids)


class FakeAuditTrail:
    """Собирает записи аудита без БД."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kwargs: Any) -> None:
        self.records.append(kwargs)

    def actions(self) -> list[str]:
        return [r["action"] for r in self.records]
