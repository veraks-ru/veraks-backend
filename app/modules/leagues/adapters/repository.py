"""SQLAlchemy-репозитории лиг и дивизионов."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.leagues.adapters.orm import (
    DivisionMembershipORM,
    DivisionORM,
    LeagueMembershipORM,
    LeagueORM,
)
from app.modules.leagues.domain.entities import (
    Division,
    DivisionMembership,
    League,
    LeagueMembership,
)


class SqlAlchemyLeagueRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, league: League) -> League:
        orm = LeagueORM.from_domain(league)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, league_id: uuid.UUID) -> League | None:
        orm = await self._session.get(LeagueORM, league_id)
        return orm.to_domain() if orm is not None else None

    async def get_by_invite_code(self, code: str) -> League | None:
        stmt = select(LeagueORM).where(LeagueORM.invite_code == code)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm is not None else None


class SqlAlchemyLeagueMembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, membership: LeagueMembership) -> LeagueMembership:
        orm = LeagueMembershipORM.from_domain(membership)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def remove(
        self, league_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        result = await self._session.execute(
            delete(LeagueMembershipORM).where(
                LeagueMembershipORM.league_id == league_id,
                LeagueMembershipORM.user_id == user_id,
            )
        )
        return (result.rowcount or 0) > 0

    async def is_member(
        self, league_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        stmt = (
            select(LeagueMembershipORM.id)
            .where(
                LeagueMembershipORM.league_id == league_id,
                LeagueMembershipORM.user_id == user_id,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None

    async def member_ids(self, league_id: uuid.UUID) -> list[uuid.UUID]:
        stmt = select(LeagueMembershipORM.user_id).where(
            LeagueMembershipORM.league_id == league_id
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def leagues_for_user(
        self, user_id: uuid.UUID
    ) -> list[uuid.UUID]:
        stmt = (
            select(LeagueMembershipORM.league_id)
            .where(LeagueMembershipORM.user_id == user_id)
            .order_by(LeagueMembershipORM.joined_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_members(self, league_id: uuid.UUID) -> int:
        stmt = select(func.count()).where(
            LeagueMembershipORM.league_id == league_id
        )
        return int((await self._session.execute(stmt)).scalar_one())


class SqlAlchemyDivisionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, division: Division) -> Division:
        orm = DivisionORM.from_domain(division)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def list_all(self) -> list[Division]:
        stmt = select(DivisionORM).order_by(DivisionORM.level.asc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [r.to_domain() for r in rows]

    async def get_by_id(self, division_id: uuid.UUID) -> Division | None:
        orm = await self._session.get(DivisionORM, division_id)
        return orm.to_domain() if orm is not None else None

    async def get_by_level(self, level: int) -> Division | None:
        stmt = select(DivisionORM).where(DivisionORM.level == level)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm is not None else None


class SqlAlchemyDivisionMembershipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_user_season(
        self, user_id: uuid.UUID, season_id: uuid.UUID
    ) -> DivisionMembership | None:
        stmt = select(DivisionMembershipORM).where(
            DivisionMembershipORM.user_id == user_id,
            DivisionMembershipORM.season_id == season_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm is not None else None

    async def list_for_season_division(
        self, season_id: uuid.UUID, division_id: uuid.UUID
    ) -> list[DivisionMembership]:
        stmt = select(DivisionMembershipORM).where(
            DivisionMembershipORM.season_id == season_id,
            DivisionMembershipORM.division_id == division_id,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [r.to_domain() for r in rows]

    async def upsert(self, membership: DivisionMembership) -> None:
        # Upsert по (user_id, season_id): один дивизион пользователя на сезон.
        stmt = (
            pg_insert(DivisionMembershipORM)
            .values(
                id=membership.id,
                user_id=membership.user_id,
                season_id=membership.season_id,
                division_id=membership.division_id,
                created_at=membership.created_at,
            )
            .on_conflict_do_update(
                constraint="uq_division_member",
                set_={"division_id": membership.division_id},
            )
        )
        await self._session.execute(stmt)
