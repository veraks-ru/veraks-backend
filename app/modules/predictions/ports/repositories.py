"""Порт репозитория прогнозов.

Прикладной слой зависит от этого протокола, а не от SQLAlchemy. Реализация —
в ``adapters/repository.py``; в тестах подставляется in-memory фейк.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.modules.predictions.domain.entities import Prediction


@runtime_checkable
class PredictionRepository(Protocol):
    """Хранилище прогнозов (один на пару ``user`` × ``event``)."""

    async def get_by_id(self, prediction_id: uuid.UUID) -> Prediction | None:
        """Прогноз по PK или ``None``."""
        ...

    async def get_for_user_event(
        self, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> Prediction | None:
        """Прогноз пользователя по событию (ключ ``UNIQUE(user_id, event_id)``)."""
        ...

    async def add(self, prediction: Prediction) -> Prediction:
        """Сохраняет новый прогноз.

        Поднимает :class:`PredictionAlreadyExistsError` при нарушении
        ``UNIQUE(user_id, event_id)`` (гонка параллельных постановок).
        """
        ...

    async def update(self, prediction: Prediction) -> Prediction:
        """Сохраняет изменения существующего прогноза (latest-wins)."""
        ...

    async def lock_for_event(self, event_id: uuid.UUID, *, now: datetime) -> int:
        """Массово блокирует прогнозы события (``is_locked = true``).

        Возвращает число затронутых строк. Идемпотентна: уже заблокированные
        не трогает. Вызывается при закрытии приёма по событию.
        """
        ...

    async def list_for_event(self, event_id: uuid.UUID) -> list[Prediction]:
        """Все прогнозы события (для последующего скоринга домена scoring)."""
        ...


class PredictionAlreadyExistsError(Exception):
    """Нарушение ``UNIQUE(user_id, event_id)`` — параллельная постановка прогноза."""
