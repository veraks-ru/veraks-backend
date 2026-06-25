"""Шлюзы resolutions к соседним доменам (events, predictions).

``EventResolutionGateway`` — единственная точка смены статуса события: владелец
конечного автомата и таблицы ``events`` остаётся в домене events, мы лишь
драйвим разрешённые переходы. ``ParticipationGateway`` отвечает на вопрос
«участвовал ли пользователь» — гейт права на оспаривание.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.modules.resolutions.application.dto import EventLifecycle


@runtime_checkable
class EventResolutionGateway(Protocol):
    """Чтение статуса события и драйв разрешённых переходов автомата events."""

    async def get_lifecycle(self, event_id: uuid.UUID) -> EventLifecycle | None:
        """Срез жизненного цикла события (статус, исход, окно, сезон) или ``None``."""
        ...

    async def fix_outcome(
        self,
        event_id: uuid.UUID,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime,
    ) -> None:
        """``closed → resolving → resolved``: фиксирует исход и открывает окно."""
        ...

    async def open_dispute(self, event_id: uuid.UUID, *, now: datetime) -> None:
        """``resolved → disputed``: на событие подано оспаривание."""
        ...

    async def dismiss_dispute(self, event_id: uuid.UUID, *, now: datetime) -> None:
        """``disputed → resolved``: спор отклонён, исход и окно сохраняются."""
        ...

    async def overturn_outcome(
        self,
        event_id: uuid.UUID,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime,
    ) -> None:
        """``disputed → resolved``: пересмотр исхода, окно открывается заново."""
        ...

    async def find_resolved_past_window(self, *, now: datetime) -> list[uuid.UUID]:
        """ID событий ``resolved`` с истёкшим окном оспаривания."""
        ...


@runtime_checkable
class ParticipationGateway(Protocol):
    """Проверка участия пользователя в событии (право на оспаривание)."""

    async def has_prediction(
        self, *, user_id: uuid.UUID, event_id: uuid.UUID
    ) -> bool:
        """Есть ли у пользователя прогноз по событию."""
        ...
