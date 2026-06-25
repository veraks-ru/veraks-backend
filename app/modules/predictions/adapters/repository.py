"""SQLAlchemy-реализация репозитория прогнозов."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.predictions.adapters.orm import PredictionORM
from app.modules.predictions.domain.entities import Prediction
from app.modules.predictions.ports.repositories import PredictionAlreadyExistsError


class SqlAlchemyPredictionRepository:
    """Хранилище прогнозов поверх асинхронной сессии SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, prediction_id: uuid.UUID) -> Prediction | None:
        """Прогноз по PK."""
        orm = await self._session.get(PredictionORM, prediction_id)
        return orm.to_domain() if orm else None

    async def get_for_user_event(
        self, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> Prediction | None:
        """Прогноз пользователя по событию (ключ ``UNIQUE(user_id, event_id)``)."""
        stmt = select(PredictionORM).where(
            PredictionORM.user_id == user_id,
            PredictionORM.event_id == event_id,
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def add(self, prediction: Prediction) -> Prediction:
        """Вставляет прогноз, разбирая нарушение ``UNIQUE(user_id, event_id)``."""
        orm = PredictionORM.from_domain(prediction)
        self._session.add(orm)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            if "user_event" in str(exc.orig) or "user_id" in str(exc.orig):
                raise PredictionAlreadyExistsError(
                    f"{prediction.user_id}/{prediction.event_id}"
                ) from exc
            raise
        return orm.to_domain()

    async def update(self, prediction: Prediction) -> Prediction:
        """Синхронизирует изменяемые поля существующего прогноза (latest-wins)."""
        orm = await self._session.get(PredictionORM, prediction.id)
        if orm is None:  # pragma: no cover — вызывается только для существующих
            raise PredictionAlreadyExistsError(str(prediction.id))
        orm.confidence_grade = prediction.confidence_grade
        orm.probability = prediction.probability
        orm.is_locked = prediction.is_locked
        orm.brier_score = prediction.brier_score
        orm.scored_at = prediction.scored_at
        orm.updated_at = prediction.updated_at
        await self._session.flush()
        return orm.to_domain()

    async def lock_for_event(self, event_id: uuid.UUID, *, now: datetime) -> int:
        """Массово блокирует прогнозы события одним UPDATE; возвращает счётчик."""
        stmt = (
            sa_update(PredictionORM)
            .where(
                PredictionORM.event_id == event_id,
                PredictionORM.is_locked.is_(False),
            )
            .values(is_locked=True, updated_at=now)
        )
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        return result.rowcount or 0

    async def list_for_event(self, event_id: uuid.UUID) -> list[Prediction]:
        """Все прогнозы события (для скоринга)."""
        stmt = select(PredictionORM).where(PredictionORM.event_id == event_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]
