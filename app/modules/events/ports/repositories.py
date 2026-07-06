"""Порты репозиториев events.

Прикладной слой зависит от этих протоколов, а не от SQLAlchemy. Реализации —
в ``adapters/repository.py``; в тестах подставляются in-memory фейки.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.modules.events.domain.entities import Category, Event, EventStatus


@dataclass(frozen=True, slots=True)
class EventFilter:
    """Критерии выборки списка событий (страница каталога)."""

    status: EventStatus | None = None
    category_id: uuid.UUID | None = None
    season_id: uuid.UUID | None = None
    limit: int = 50
    offset: int = 0


@runtime_checkable
class EventRepository(Protocol):
    """Хранилище событий."""

    async def get_by_id(self, event_id: uuid.UUID) -> Event | None:
        """Событие по PK или ``None``."""
        ...

    async def add(self, event: Event) -> Event:
        """Сохраняет новое событие."""
        ...

    async def update(self, event: Event) -> Event:
        """Сохраняет изменения существующего события."""
        ...

    async def list(
        self, criteria: EventFilter, *, include_unlisted: bool = False
    ) -> list[Event]:
        """События по фильтру (сортировка — ``closes_at``, tie-break ``id``).

        ``include_unlisted=False`` скрывает черновики и предложения на модерации.
        """
        ...

    async def list_open_due(self, now: datetime) -> Sequence[Event]:
        """Открытые события с истёкшим ``closes_at`` (для авто-закрытия приёма)."""
        ...


@runtime_checkable
class CategoryRepository(Protocol):
    """Хранилище категорий (дерево)."""

    async def get_by_id(self, category_id: uuid.UUID) -> Category | None:
        """Категория по PK или ``None``."""
        ...

    async def get_by_slug(self, slug: str) -> Category | None:
        """Категория по slug или ``None``."""
        ...

    async def exists(self, category_id: uuid.UUID) -> bool:
        """Проверка существования категории (для FK события)."""
        ...

    async def add(self, category: Category) -> Category:
        """Сохраняет категорию.

        Поднимает :class:`CategorySlugTakenError` при нарушении ``UNIQUE(slug)``.
        """
        ...

    async def list_all(self) -> list[Category]:
        """Все категории (дерево собирается на чтении из плоского списка)."""
        ...
