"""Реализация ``PredictionGuard`` для домена seasons.

Отвечает на единственный вопрос: делал ли кто-нибудь прогноз по событиям
сезона. От этого зависит, можно ли ещё исправить неудачно замороженные правила
активного сезона (см. ``seasons.application.use_cases.RepairSeasonRules``).

Живёт в predictions, а не в seasons: прогнозы — его данные, а направление
зависимостей идёт внутрь. Связывается с seasons в composition root — там же,
где ``ResolutionDisputeGuard``.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import EventORM
from app.modules.predictions.adapters.orm import PredictionORM


class SeasonPredictionGuard:
    """Есть ли хоть один прогноз по событиям сезона."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_predictions(self, season_id: uuid.UUID) -> bool:
        """``True``, если по любому событию сезона есть прогноз.

        ``EXISTS`` вместо ``count``: нужен факт наличия, а не число — на
        большом сезоне пересчитывать все строки незачем.
        """
        stmt = select(
            exists().where(
                PredictionORM.event_id == EventORM.id,
                EventORM.season_id == season_id,
            )
        )
        return bool((await self._session.execute(stmt)).scalar())
