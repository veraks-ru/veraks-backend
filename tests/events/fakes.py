"""In-memory фейки портов events для изолированного тестирования.

Реализуют те же протоколы, что и продакшн-адаптеры, но без I/O — это
позволяет юнит-тестировать use-cases и интеграционно гонять эндпоинты без
Postgres и без реальных часов.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from app.modules.events.domain.entities import Category, Event
from app.modules.events.domain.errors import CategorySlugTakenError
from app.modules.events.ports.repositories import EventFilter
from app.shared.audit.domain.entities import AuditActorType, AuditEntry


class FakeClock:
    """Часы с фиксированным (управляемым) временем."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        """Тестовый помощник: перевести часы."""
        self._now = now


class InMemoryEventRepository:
    """Хранилище событий в памяти."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Event] = {}

    async def get_by_id(self, event_id: uuid.UUID) -> Event | None:
        return self._clone(self._by_id.get(event_id))

    async def add(self, event: Event) -> Event:
        self._by_id[event.id] = self._clone(event)
        return self._clone(event)

    async def update(self, event: Event) -> Event:
        self._by_id[event.id] = self._clone(event)
        return self._clone(event)

    async def list(self, criteria: EventFilter) -> list[Event]:
        items = [self._clone(e) for e in self._by_id.values()]
        if criteria.status is not None:
            items = [e for e in items if e.status is criteria.status]
        if criteria.category_id is not None:
            items = [e for e in items if e.category_id == criteria.category_id]
        if criteria.season_id is not None:
            items = [e for e in items if e.season_id == criteria.season_id]
        items.sort(key=lambda e: e.window.closes_at)
        return items[criteria.offset : criteria.offset + criteria.limit]

    @staticmethod
    def _clone(event: Event | None) -> Event | None:
        """Копия, чтобы внешние мутации не текли в хранилище."""
        if event is None:
            return None
        return Event(
            id=event.id,
            title=event.title,
            description=event.description,
            category_id=event.category_id,
            created_by=event.created_by,
            window=event.window,
            resolution_source=event.resolution_source,
            resolution_criteria=event.resolution_criteria,
            season_id=event.season_id,
            status=event.status,
            outcome=event.outcome,
            resolved_at=event.resolved_at,
            dispute_window_ends_at=event.dispute_window_ends_at,
            created_at=event.created_at,
            updated_at=event.updated_at,
        )


class InMemoryCategoryRepository:
    """Хранилище категорий в памяти с эмуляцией ``UNIQUE(slug)``."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Category] = {}

    def seed(self, category: Category) -> Category:
        """Тестовый помощник: положить категорию напрямую."""
        self._by_id[category.id] = category
        return category

    async def get_by_id(self, category_id: uuid.UUID) -> Category | None:
        return self._by_id.get(category_id)

    async def get_by_slug(self, slug: str) -> Category | None:
        for category in self._by_id.values():
            if category.slug.lower() == slug.lower():
                return category
        return None

    async def exists(self, category_id: uuid.UUID) -> bool:
        return category_id in self._by_id

    async def add(self, category: Category) -> Category:
        for existing in self._by_id.values():
            if existing.slug.lower() == category.slug.lower():
                raise CategorySlugTakenError(category.slug)
        self._by_id[category.id] = category
        return category

    async def list_all(self) -> list[Category]:
        return sorted(self._by_id.values(), key=lambda c: c.slug)


class FakeAuditTrail:
    """Запоминает записи аудита (без реальной хеш-цепочки)."""

    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        self.records.append(
            {
                "actor_id": actor_id,
                "actor_type": actor_type,
                "action": action,
                "entity_type": entity_type,
                "entity_id": entity_id,
                "before": before,
                "after": after,
            }
        )
        return AuditEntry(
            occurred_at=datetime(2026, 1, 1),  # noqa: DTZ001 — фейк, время не важно
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            hash="fake",
        )

    def actions(self) -> list[str]:
        """Список зафиксированных action'ов (для ассертов)."""
        return [r["action"] for r in self.records]
