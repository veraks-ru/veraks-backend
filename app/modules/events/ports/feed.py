"""Read-model порт ленты открытых событий для свайпа (дизайн-спека §3).

Домен и порты events намеренно не знают о модуле predictions: сводка толпы
приходит уже агрегированной, а распределение по грейдам — ``Mapping[str, int]``
по строковым значениям (не по enum ``ConfidenceGrade``). Единственное место,
которому разрешено читать таблицу ``predictions`` напрямую, — реализация
порта в ``adapters/feed_reader.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.modules.events.domain.entities import Event
from app.modules.events.domain.value_objects import FeedCursor


@dataclass(frozen=True, slots=True)
class FeedCategoryRef:
    """Минимальная проекция категории для карточки ленты."""

    id: uuid.UUID
    slug: str
    title: str


@dataclass(frozen=True, slots=True)
class FeedCrowd:
    """Сводка толпы по событию («сигнал толпы» — см. ``GetEventPredictionSummary``).

    ``distribution`` — все пять грейдов уверенности по строковому ключу,
    отсутствующие голоса — нули. ``mean_probability`` — ``None`` при нуле
    прогнозов.
    """

    total_count: int
    distribution: Mapping[str, int]
    mean_probability: Decimal | None


@dataclass(frozen=True, slots=True)
class EventFeedItem:
    """Одна карточка ленты: событие + его категория + сводка толпы."""

    event: Event
    category: FeedCategoryRef
    crowd: FeedCrowd


@dataclass(frozen=True, slots=True)
class FeedQuery:
    """Параметры страницы ленты.

    ``exclude_predicted_by`` — id зрителя, чьи уже предсказанные события надо
    исключить анти-джойном; ``None`` у гостя — исключений нет.
    """

    now: datetime
    limit: int
    category_id: uuid.UUID | None = None
    after: FeedCursor | None = None
    exclude_predicted_by: uuid.UUID | None = None


@runtime_checkable
class EventFeedReader(Protocol):
    """Read-model страницы ленты открытых событий."""

    async def page(self, query: FeedQuery) -> list[EventFeedItem]:
        """Возвращает до ``query.limit`` карточек, отсортированных по ``(closes_at, id)``."""
        ...
