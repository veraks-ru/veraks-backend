"""In-memory фейки портов predictions для изолированного тестирования.

Реализуют те же протоколы, что и продакшн-адаптеры, но без I/O — это
позволяет юнит-тестировать use-cases и интеграционно гонять эндпоинты без
Postgres, без реальных часов и без домена events/аудита.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from app.modules.predictions.application.dto import PredictionAuditEntry
from app.modules.predictions.domain.entities import Prediction
from app.modules.predictions.domain.value_objects import EventSnapshot
from app.modules.predictions.ports.repositories import PredictionAlreadyExistsError


class FakeClock:
    """Часы с фиксированным (управляемым) временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        """Тестовый помощник: перевести часы."""
        self._now = now


class FakeEventGateway:
    """Шлюз событий, отдающий заранее заданные снимки."""

    def __init__(self, snapshots: list[EventSnapshot] | None = None) -> None:
        self._by_id: dict[uuid.UUID, EventSnapshot] = {
            s.event_id: s for s in (snapshots or [])
        }

    def set(self, snapshot: EventSnapshot) -> None:
        """Тестовый помощник: положить/заменить снимок события."""
        self._by_id[snapshot.event_id] = snapshot

    async def get_snapshot(self, event_id: uuid.UUID) -> EventSnapshot | None:
        return self._by_id.get(event_id)


class FakeAuditRecorder:
    """Собирает записи истории в список (для проверок в тестах)."""

    def __init__(self) -> None:
        self.entries: list[PredictionAuditEntry] = []

    async def record(self, entry: PredictionAuditEntry) -> None:
        self.entries.append(entry)


class InMemoryPredictionRepository:
    """Хранилище прогнозов в памяти с эмуляцией ``UNIQUE(user_id, event_id)``."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Prediction] = {}

    def seed(self, prediction: Prediction) -> Prediction:
        """Тестовый помощник: положить прогноз напрямую (синхронно)."""
        self._by_id[prediction.id] = self._clone(prediction)
        return prediction

    async def get_by_id(self, prediction_id: uuid.UUID) -> Prediction | None:
        return self._clone(self._by_id.get(prediction_id))

    async def get_for_user_event(
        self, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> Prediction | None:
        for prediction in self._by_id.values():
            if prediction.user_id == user_id and prediction.event_id == event_id:
                return self._clone(prediction)
        return None

    async def add(self, prediction: Prediction) -> Prediction:
        for existing in self._by_id.values():
            if (
                existing.user_id == prediction.user_id
                and existing.event_id == prediction.event_id
            ):
                raise PredictionAlreadyExistsError(
                    f"{prediction.user_id}/{prediction.event_id}"
                )
        self._by_id[prediction.id] = self._clone(prediction)
        return self._clone(prediction)

    async def update(self, prediction: Prediction) -> Prediction:
        self._by_id[prediction.id] = self._clone(prediction)
        return self._clone(prediction)

    async def lock_for_event(self, event_id: uuid.UUID, *, now: datetime) -> int:
        count = 0
        for prediction in self._by_id.values():
            if prediction.event_id == event_id and not prediction.is_locked:
                prediction.is_locked = True
                prediction.updated_at = now
                count += 1
        return count

    async def list_for_event(self, event_id: uuid.UUID) -> list[Prediction]:
        return [
            self._clone(p)
            for p in self._by_id.values()
            if p.event_id == event_id
        ]

    @staticmethod
    def _clone(prediction: Prediction | None) -> Prediction | None:
        """Копия, чтобы внешние мутации не текли в хранилище."""
        if prediction is None:
            return None
        return Prediction(
            id=prediction.id,
            user_id=prediction.user_id,
            event_id=prediction.event_id,
            confidence_grade=prediction.confidence_grade,
            probability=prediction.probability,
            is_locked=prediction.is_locked,
            brier_score=prediction.brier_score,
            scored_at=prediction.scored_at,
            created_at=prediction.created_at,
            updated_at=prediction.updated_at,
        )
