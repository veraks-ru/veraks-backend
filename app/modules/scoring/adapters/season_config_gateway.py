"""Адаптер ``SeasonConfigGateway`` поверх таблицы ``seasons`` (монолит, БД).

Читает таблицу сезонов напрямую — это интеграционный шов, сохраняющий
направление ``scoring → seasons`` без обратной зависимости. При выносе seasons
в отдельный сервис заменяется сетевым контрактом, порт и use-cases не меняются.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.scoring.application.dto import SeasonConfigView
from app.modules.seasons.adapters.orm import SeasonORM
from app.modules.seasons.domain.value_objects import LeagueConfig


class SqlAlchemySeasonConfigGateway:
    """Чтение сезона для scoring: резолв slug и снапшот ``LeagueConfig``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve_slug(self, slug: str) -> uuid.UUID | None:
        stmt = select(SeasonORM.id).where(SeasonORM.slug == slug)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_config(self, season_id: uuid.UUID) -> SeasonConfigView | None:
        season = await self._session.get(SeasonORM, season_id)
        if season is None:
            return None
        config = (
            LeagueConfig.from_dict(season.league_config)
            if season.league_config is not None
            else None
        )
        return SeasonConfigView(status=season.status, config=config)
