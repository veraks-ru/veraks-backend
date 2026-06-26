"""Порты-шлюзы scoring к данным других доменов (predictions/events).

Прикладной слой зависит от этих протоколов, а не от SQLAlchemy или доменов
events/predictions напрямую. В монолите адаптеры читают соответствующие
таблицы; при выносе в сервис — заменяются сетевым контрактом без изменения
use-cases.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.modules.scoring.application.dto import EventScoringStatus, PredictionScore
from app.modules.scoring.domain.value_objects import ResolvedEvent


@runtime_checkable
class EventScoringGateway(Protocol):
    """Чтение разрешённых событий, их голосов и калибровочных записей."""

    async def get_status(self, event_id: uuid.UUID) -> EventScoringStatus:
        """Готовность события к скорингу (найдено/разрешено/финально)."""
        ...

    async def get_resolved_event(self, event_id: uuid.UUID) -> ResolvedEvent | None:
        """Полное разрешённое событие с заблокированными прогнозами или ``None``."""
        ...

    async def list_resolved_events(
        self, *, season_id: uuid.UUID | None = None
    ) -> list[ResolvedEvent]:
        """Все разрешённые события (опционально — в рамках сезона) для пересчёта."""
        ...

    async def list_user_calibration_entries(
        self, user_id: uuid.UUID
    ) -> list[tuple[float, int]]:
        """Пары ``(номинальная вероятность, исход)`` по засчитанным прогнозам."""
        ...

    async def list_season_calibration_entries(
        self, season_id: uuid.UUID
    ) -> list[tuple[float, int]]:
        """Пары ``(номинал, исход)`` по всем засчитанным прогнозам сезона.

        Популяционная выборка для межсезонной рекалибровки маппинга градаций.
        """
        ...


@runtime_checkable
class PredictionScoreWriter(Protocol):
    """Запись пер-прогнозного Brier обратно в ``predictions`` (один на событие)."""

    async def save_event_scores(
        self,
        event_id: uuid.UUID,
        scores: Sequence[PredictionScore],
        *,
        now: datetime,
    ) -> int:
        """Проставляет ``brier_score``/``scored_at``; возвращает число строк.

        Идемпотентна по событию: повтор с тем же исходом даёт те же значения
        (latest-wins). При overturn'е исход меняется — новые значения
        перезаписывают старые (ре-скоринг).
        """
        ...
