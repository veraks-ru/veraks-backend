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
from app.modules.leagues.domain.promotion import (
    compute_initial_placement,
    compute_promotion,
)
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
from app.shared.audit.domain.entities import AuditActorType
from app.shared.audit.ports.audit_trail import AuditTrail


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


# ── Админская модерация лиг ──────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class LeaguePage:
    """Страница списка лиг для админки: элементы + общее число."""

    items: list[LeagueSummary]
    total: int


class ListAllLeagues:
    """Все приватные лиги с числом участников (admin).

    Владелец видит только свои лиги, поэтому лига с оскорбительным названием
    иначе недосягаема для модерации.
    """

    def __init__(
        self,
        *,
        leagues: LeagueRepository,
        memberships: LeagueMembershipRepository,
    ) -> None:
        self._leagues = leagues
        self._memberships = memberships

    async def execute(self, *, limit: int = 50, offset: int = 0) -> LeaguePage:
        found = await self._leagues.list_all(limit=limit, offset=offset)
        items = [
            LeagueSummary(
                league=league,
                members=await self._memberships.count_members(league.id),
            )
            for league in found
        ]
        return LeaguePage(items=items, total=await self._leagues.count_all())


class RenameLeague:
    """Переименовать лигу (admin) — модерация недопустимых названий."""

    def __init__(self, *, leagues: LeagueRepository, audit: AuditTrail) -> None:
        self._leagues = leagues
        self._audit = audit

    async def execute(
        self, *, actor_id: uuid.UUID, league_id: uuid.UUID, name: str
    ) -> League:
        before = await self._leagues.get_by_id(league_id)
        if before is None:
            raise LeagueNotFoundError("Лига не найдена")
        # Снимаем прежнее имя строкой ДО правки: репозиторий вправе вернуть ту
        # же сущность, что потом мутирует, и тогда диф аудита схлопнулся бы.
        before_name = before.name
        # Валидация та же, что при создании (пустое/слишком длинное имя).
        clean = League.create(
            name=name, owner_id=before.owner_id, invite_code=before.invite_code
        ).name
        renamed = await self._leagues.rename(league_id, clean)
        await self._audit.record(
            actor_id=actor_id,
            actor_type=AuditActorType.ADMIN,
            action="league.renamed",
            entity_type="league",
            entity_id=league_id,
            before={"name": before_name},
            after={"name": renamed.name},
        )
        return renamed


class DeleteLeague:
    """Удалить лигу вместе с участием (admin).

    Обычное удаление, а не мягкое: лига не связана ни с прогнозами, ни с
    призовым зачётом, ни с деньгами — стирать нечего, кроме самой группы.
    Факт удаления остаётся в append-only аудите.
    """

    def __init__(self, *, leagues: LeagueRepository, audit: AuditTrail) -> None:
        self._leagues = leagues
        self._audit = audit

    async def execute(
        self, *, actor_id: uuid.UUID, league_id: uuid.UUID
    ) -> None:
        league = await self._leagues.get_by_id(league_id)
        if league is None:
            raise LeagueNotFoundError("Лига не найдена")
        await self._leagues.delete(league_id)
        await self._audit.record(
            actor_id=actor_id,
            actor_type=AuditActorType.ADMIN,
            action="league.deleted",
            entity_type="league",
            entity_id=league_id,
            before={"name": league.name, "owner_id": str(league.owner_id)},
            after={},
        )


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


class SeedSeasonDivisions:
    """Первичный посев дивизионов для сезона без предшественника.

    :class:`ApplyPromotionRelegation` строит расстановку из membership прошлого
    сезона — для самого первого сезона брать неоткуда, и лестница не стартует.
    Здесь участники раскладываются напрямую: все активные аккаунты, у которых
    ещё нет дивизиона в этом сезоне.

    Идемпотентно и неразрушительно: уже назначенных не трогаем (иначе повтор
    затёр бы результаты промоции), если явно не передан ``overwrite=True``.
    Порядок для ``even_split`` — по глобальному рейтингу; у кого его нет,
    уходят в конец списка и оказываются в низших дивизионах.
    """

    def __init__(
        self,
        *,
        divisions: DivisionRepository,
        memberships: DivisionMembershipRepository,
        standings: StandingsGateway,
        users: UserLookup,
    ) -> None:
        self._divisions = divisions
        self._memberships = memberships
        self._standings = standings
        self._users = users

    async def execute(
        self,
        *,
        season_id: uuid.UUID,
        even_split: bool = False,
        overwrite: bool = False,
    ) -> int:
        divisions = await self._divisions.list_all()
        if not divisions:
            return 0
        by_level = {d.level: d for d in divisions}
        num_levels = max(by_level)

        candidates = await self._users.active_user_ids()
        if not overwrite:
            candidates = [
                user_id
                for user_id in candidates
                if await self._memberships.get_for_user_season(user_id, season_id)
                is None
            ]
        if not candidates:
            return 0

        ranked = (
            await self._ranked_by_global(candidates) if even_split else candidates
        )
        placements = compute_initial_placement(
            ranked, num_levels=num_levels, even_split=even_split
        )

        written = 0
        for user_id, level in placements.items():
            target = by_level.get(level)
            if target is None:
                continue
            await self._memberships.upsert(
                DivisionMembership(
                    user_id=user_id, season_id=season_id, division_id=target.id
                )
            )
            written += 1
        return written

    async def _ranked_by_global(
        self, user_ids: list[uuid.UUID]
    ) -> list[uuid.UUID]:
        """Сортирует по глобальному рейтингу; безрейтинговые — в конец."""
        rows = _rank_rows(await self._standings.global_rows(user_ids))
        ordered = [row.user_id for row in rows]
        seen = set(ordered)
        return ordered + [uid for uid in user_ids if uid not in seen]


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
            target = by_level.get(level)
            if target is None:
                continue
            await self._memberships.upsert(
                DivisionMembership(
                    user_id=user_id,
                    season_id=next_season_id,
                    division_id=target.id,
                )
            )
            written += 1
        return written
