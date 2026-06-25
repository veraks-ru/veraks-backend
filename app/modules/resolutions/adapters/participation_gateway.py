"""Шлюз проверки участия пользователя в событии (право на оспаривание).

Читает таблицу ``predictions`` напрямую: оспаривать вправе только тот, кто
поставил прогноз по событию.

TODO(resolutions-predictions): прямое чтение таблицы соседнего домена в
монолите — допустимо до выделения публичного порта у predictions; заменить
контрактом при выносе домена в сервис.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.predictions.adapters.orm import PredictionORM


class SqlAlchemyParticipationGateway:
    """Проверка наличия прогноза пользователя по событию."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_prediction(
        self, *, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> bool:
        """Есть ли у пользователя прогноз по событию."""
        stmt = select(
            exists().where(
                PredictionORM.user_id == user_id,
                PredictionORM.event_id == event_id,
            )
        )
        return bool((await self._session.execute(stmt)).scalar())
