"""Use-cases лиг и дивизионов.

Приватные лиги: создание/вступление/выход/список/лидерборд. Дивизионы:
лидерборд уровня, свой дивизион, применение повышения/понижения между сезонами.
Зависимости — только через порты.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.leagues.domain.entities import (
    DivisionMembership,
    League,
    LeagueMembership,
)
from app.modules.leagues.domain.errors import (
    DivisionNotFoundError,
    LeagueNotFoundError,
)
from app.modules.leagues.domain.promotion import compute_promotion
from app.modules.leagues.ports.repositories import (
    DivisionMembershipRepository,
    DivisionRepository,
    InviteCodeGenerator,
    LeagueMembershipRepository,
    LeagueRepository,
    StandingRow,
    StandingsGateway,
    UserLookup,
)


def _rank_rows(rows: list[StandingRow]) -> list[StandingRow]:
    """Сортирует по skill_score (None в конец) и проставляет позиции 1..n."""
    ordered = sorted(
        rows,
        key=lambda r: (r.skill_score is None, -(r.skill_score or 0)),
    )
    from dataclasses import replace

    return [replace(r, rank=i) for i, r in enumerate(ordered, start=1)]


# ── Приватные лиги ───────────────────────────────────────────────────────────


class CreateLeague:
    """Создать приватную лигу; владелец сразу становится участником."""

    def __init__(
        self,
        *,
        leagues: LeagueRepository,
        memberships: LeagueMembershipRepository,
        codes: InviteCodeGenerator,
    ) -> None:
        self._leagues = leagues
        self._memberships = memberships
        self._codes = codes

    async def execute(self, *, owner_id: uuid.UUID, name: str) -> League:
        league = League.create(
            name=name, owner_id=owner_id, invite_code=self._codes.generate()
        )
        saved = await self._leagues.add(league)
        await self._memberships.add(
            LeagueMembership(league_id=saved.id, user_id=owner_id)
        )
        return saved


class JoinLeague:
    """Вступить в лигу по коду приглашения (идемпотентно)."""

    def __init__(
        self,
        *,
        leagues: LeagueRepository,
        memberships: LeagueMembershipRepository,
    ) -> None:
        self._leagues = leagues
        self._memberships = memberships

    async def execute(self, *, user_id: uuid.UUID, invite_code: str) -> League:
        league = await self._leagues.get_by_invite_code(invite_code.strip())
        if league is None:
            raise LeagueNotFoundError("Лига по коду не найдена")
        if not await self._memberships.is_member(league.id, user_id):
            await self._memberships.add(
                LeagueMembership(league_id=league.id, user_id=user_id)
            )
        return league


class LeaveLeague:
    """Выйти из лиги (владелец тоже может; лига остаётся)."""

    def __init__(self, *, memberships: LeagueMembershipRepository) -> None:
        self._memberships = memberships

    async def execute(
        self, *, user_id: uuid.UUID, league_id: uuid.UUID
    ) -> bool:
        return await self._memberships.remove(league_id, user_id)


@dataclass(frozen=True, slots=True)
class LeagueSummary:
    league: League
    members: int


class ListMyLeagues:
    """Лиги пользователя с числом участников."""

    def __init__(
        self,
        *,
        leagues: LeagueRepository,
        memberships: LeagueMembershipRepository,
    ) -> None:
        self._leagues = leagues
        self._memberships = memberships

    async def execute(self, *, user_id: uuid.UUID) -> list[LeagueSummary]:
        league_ids = await self._memberships.leagues_for_user(user_id)
        out: list[LeagueSummary] = []
        for lid in league_ids:
            league = await self._leagues.get_by_id(lid)
            if league is None:
                continue
            out.append(
                LeagueSummary(
                    league=league,
                    members=await self._memberships.count_members(lid),
                )
            )
        return out


@dataclass(frozen=True, slots=True)
class LeagueStandings:
    league: League
    is_member: bool
    rows: list[StandingRow]


class GetLeagueStandings:
    """Лидерборд лиги: участники, ранжированные по глобальному skill_score."""

    def __init__(
        self,
        *,
        leagues: LeagueRepository,
        memberships: LeagueMembershipRepository,
        standings: StandingsGateway,
    ) -> None:
        self._leagues = leagues
        self._memberships = memberships
        self._standings = standings

    async def execute(
        self, *, league_id: uuid.UUID, viewer_id: uuid.UUID | None = None
    ) -> LeagueStandings:
        league = await self._leagues.get_by_id(league_id)
        if league is None:
            raise LeagueNotFoundError("Лига не найдена")
        member_ids = await self._memberships.member_ids(league_id)
        rows = _rank_rows(await self._standings.global_rows(member_ids))
        is_member = (
            viewer_id is not None
            and await self._memberships.is_member(league_id, viewer_id)
        )
        return LeagueStandings(league=league, is_member=is_member, rows=rows)


# ── Дивизионы ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class DivisionStandings:
    division_level: int
    division_title: str
    season_id: uuid.UUID
    rows: list[StandingRow]


class GetDivisionStandings:
    """Лидерборд дивизиона в сезоне (участники уровня, ранжированные)."""

    def __init__(
        self,
        *,
        divisions: DivisionRepository,
        memberships: DivisionMembershipRepository,
        standings: StandingsGateway,
    ) -> None:
        self._divisions = divisions
        self._memberships = memberships
        self._standings = standings

    async def execute(
        self, *, season_id: uuid.UUID, level: int
    ) -> DivisionStandings:
        division = await self._divisions.get_by_level(level)
        if division is None:
            raise DivisionNotFoundError(f"Дивизион уровня {level} не найден")
        members = await self._memberships.list_for_season_division(
            season_id, division.id
        )
        rows = _rank_rows(
            await self._standings.global_rows([m.user_id for m in members])
        )
        return DivisionStandings(
            division_level=division.level,
            division_title=division.title,
            season_id=season_id,
            rows=rows,
        )


class ApplyPromotionRelegation:
    """Разносит участников дивизионов на следующий сезон по итогам текущего.

    Читает состав каждого дивизиона в завершённом сезоне, ранжирует по итоговым
    рейтингам, применяет :func:`compute_promotion` и пишет membership на новый
    сезон. Идемпотентно на уровне upsert (повтор перезапишет те же назначения).
    """

    def __init__(
        self,
        *,
        divisions: DivisionRepository,
        memberships: DivisionMembershipRepository,
        standings: StandingsGateway,
    ) -> None:
        self._divisions = divisions
        self._memberships = memberships
        self._standings = standings

    async def execute(
        self,
        *,
        finished_season_id: uuid.UUID,
        next_season_id: uuid.UUID,
        promote: int = 2,
        relegate: int = 2,
    ) -> int:
        divisions = await self._divisions.list_all()
        if not divisions:
            return 0
        by_level = {d.level: d for d in divisions}
        num_levels = max(by_level)

        standings_by_level: dict[int, list[uuid.UUID]] = {}
        for division in divisions:
            members = await self._memberships.list_for_season_division(
                finished_season_id, division.id
            )
            ranked = await self._standings.season_ranked_ids(
                finished_season_id, [m.user_id for m in members]
            )
            standings_by_level[division.level] = ranked

        placements = compute_promotion(
            standings_by_level,
            num_levels=num_levels,
            promote=promote,
            relegate=relegate,
        )

        # Новички: у кого есть сезонный рейтинг, но не было дивизиона — в низший.
        lowest_level = num_levels
        rated = await self._standings.season_rated_ids(finished_season_id)
        for user_id in rated:
            placements.setdefault(user_id, lowest_level)

        written = 0
        for user_id, level in placements.items():
            division = by_level.get(level)
            if division is None:
                continue
            await self._memberships.upsert(
                DivisionMembership(
                    user_id=user_id,
                    season_id=next_season_id,
                    division_id=division.id,
                )
            )
            written += 1
        return written
