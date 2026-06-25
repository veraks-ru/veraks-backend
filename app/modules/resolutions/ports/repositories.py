"""Порты хранилищ resolutions: решения, споры, диспатчи скоринга.

``ResolutionRepository`` — INSERT-only (журнал неизменяем). ``DisputeRepository``
допускает ``update`` (споры — изменяемый жизненный цикл).
``ScoringDispatchRepository`` — маркер «скоринг по резолюции поставлен»,
ограничивает скан воркера и даёт идемпотентность постановки задачи.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.modules.resolutions.domain.entities import Dispute, Resolution


@runtime_checkable
class ResolutionRepository(Protocol):
    """Append-only журнал решений по событиям."""

    async def add(self, resolution: Resolution) -> Resolution:
        """Вставляет новое решение (без UPDATE)."""
        ...

    async def current_final(self, event_id: uuid.UUID) -> Resolution | None:
        """Текущее (последнее финальное) решение события или ``None``."""
        ...

    async def list_for_event(self, event_id: uuid.UUID) -> list[Resolution]:
        """Полная история решений события (для реконструкции пересмотров)."""
        ...


@runtime_checkable
class DisputeRepository(Protocol):
    """Хранилище споров (изменяемый статус)."""

    async def add(self, dispute: Dispute) -> Dispute:
        """Вставляет новый спор."""
        ...

    async def get_by_id(self, dispute_id: uuid.UUID) -> Dispute | None:
        """Спор по PK или ``None``."""
        ...

    async def update(self, dispute: Dispute) -> Dispute:
        """Сохраняет изменения статуса/решения спора."""
        ...

    async def list_for_event(self, event_id: uuid.UUID) -> list[Dispute]:
        """Все споры события (новые выше)."""
        ...

    async def has_open_for_event(self, event_id: uuid.UUID) -> bool:
        """Есть ли по событию незакрытый спор."""
        ...

    async def has_open_in_season(self, season_id: uuid.UUID) -> bool:
        """Есть ли незакрытые споры по событиям сезона (для ``DisputeGuard``)."""
        ...


@runtime_checkable
class ScoringDispatchRepository(Protocol):
    """Маркеры поставленных в скоринг резолюций (идемпотентность воркера)."""

    async def exists(self, resolution_id: uuid.UUID) -> bool:
        """Был ли уже поставлен скоринг по этой резолюции."""
        ...

    async def add(
        self, *, resolution_id: uuid.UUID, event_id: uuid.UUID, now: datetime
    ) -> bool:
        """Атомарно фиксирует диспатч. ``False``, если уже существовал (гонка)."""
        ...
