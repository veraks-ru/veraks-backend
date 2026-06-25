"""SQLAlchemy-репозиторий сезонов и журнала финализаций.

``lock_for_finalize`` берёт строку сезона ``FOR UPDATE`` — блокировка держится
до конца транзакции вызывающего и сериализует параллельные финализации одного
сезона (дизайн §6.1). ``append_finalization`` пишет родителя и строки-участники
в текущей транзакции; коммитит вызывающий (request-scope или worker-scope).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.seasons.adapters.orm import (
    SeasonFinalizationEntryORM,
    SeasonFinalizationORM,
    SeasonORM,
)
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import (
    SeasonFinalization,
    SeasonFinalizationEntry,
)


class SqlAlchemySeasonRepository:
    """Хранилище сезонов поверх асинхронной сессии SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, season: Season) -> None:
        self._session.add(SeasonORM.from_domain(season))
        await self._session.flush()

    async def get_by_id(self, season_id: uuid.UUID) -> Season | None:
        orm = await self._session.get(SeasonORM, season_id)
        return orm.to_domain() if orm else None

    async def get_by_slug(self, slug: str) -> Season | None:
        stmt = select(SeasonORM).where(SeasonORM.slug == slug)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def list(self, *, status: SeasonStatus | None = None) -> list[Season]:
        stmt = select(SeasonORM).order_by(SeasonORM.starts_at.asc())
        if status is not None:
            stmt = stmt.where(SeasonORM.status == status)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]

    async def update(self, season: Season) -> None:
        orm = await self._session.get(SeasonORM, season.id)
        if orm is None:  # pragma: no cover - вызывающий гарантирует существование
            self._session.add(SeasonORM.from_domain(season))
            await self._session.flush()
            return
        orm.slug = season.slug
        orm.title = season.title
        orm.starts_at = season.starts_at
        orm.ends_at = season.ends_at
        orm.status = season.status
        orm.league_config = (
            season.league_config.to_dict() if season.league_config is not None else None
        )
        orm.updated_at = season.updated_at
        await self._session.flush()

    async def lock_for_finalize(self, season_id: uuid.UUID) -> Season | None:
        stmt = select(SeasonORM).where(SeasonORM.id == season_id).with_for_update()
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def append_finalization(
        self,
        finalization: SeasonFinalization,
        entries: Sequence[SeasonFinalizationEntry],
    ) -> None:
        self._session.add(SeasonFinalizationORM.from_domain(finalization))
        for entry in entries:
            self._session.add(
                SeasonFinalizationEntryORM.from_domain(finalization.id, entry)
            )
        await self._session.flush()
