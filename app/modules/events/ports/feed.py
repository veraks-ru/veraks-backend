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
class FeedViewerAnswer:
    """Ответ самого зрителя по событию — только в режиме «мои ответы».

    ``confidence_grade`` — строковое значение грейда (домен predictions сюда
    не тянем, как и в :class:`FeedCrowd`).
    """

    confidence_grade: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class EventFeedItem:
    """Одна карточка ленты: событие + его категория + сводка толпы.

    ``viewer_answer`` заполнен только в режиме «мои ответы»
    (``FeedQuery.only_predicted_by``); в обычной ленте всегда ``None`` —
    там по построению нет событий, где зритель уже высказался.
    """

    event: Event
    category: FeedCategoryRef
    crowd: FeedCrowd
    viewer_answer: FeedViewerAnswer | None = None


@dataclass(frozen=True, slots=True)
class FeedQuery:
    """Параметры страницы ленты.

    ``exclude_predicted_by`` — id зрителя, чьи уже предсказанные события надо
    исключить анти-джойном; ``None`` у гостя — исключений нет.

    ``only_predicted_by`` — обратный режим «мои ответы»: оставить ТОЛЬКО
    события с прогнозом этого зрителя и приложить сам прогноз
    (:attr:`EventFeedItem.viewer_answer`). Взаимоисключающе с
    ``exclude_predicted_by``; окно приёма и порядок те же — лента просто
    продолжается, когда новые карточки кончились.
    """

    now: datetime
    limit: int
    category_id: uuid.UUID | None = None
    after: FeedCursor | None = None
    exclude_predicted_by: uuid.UUID | None = None
    only_predicted_by: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.exclude_predicted_by is not None and self.only_predicted_by is not None:
            raise ValueError("exclude_predicted_by и only_predicted_by взаимоисключающи")


@runtime_checkable
class EventFeedReader(Protocol):
    """Read-model страницы ленты открытых событий."""

    async def page(self, query: FeedQuery) -> list[EventFeedItem]:
        """Возвращает до ``query.limit`` карточек, отсортированных по ``(closes_at, id)``."""
        ...
