"""SQLAlchemy-реализации репозиториев events."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import CategoryORM, EventORM
from app.modules.events.domain.entities import Category, Event, EventStatus
from app.modules.events.domain.errors import CategorySlugTakenError
from app.modules.events.ports.repositories import EventFilter


class SqlAlchemyEventRepository:
    """Хранилище событий поверх асинхронной сессии SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, event_id: uuid.UUID) -> Event | None:
        """Событие по PK."""
        orm = await self._session.get(EventORM, event_id)
        return orm.to_domain() if orm else None

    async def add(self, event: Event) -> Event:
        """Вставляет новое событие."""
        orm = EventORM.from_domain(event)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def update(self, event: Event) -> Event:
        """Синхронизирует изменяемые поля существующего события."""
        orm = await self._session.get(EventORM, event.id)
        if orm is None:  # pragma: no cover — вызывается только для существующих
            raise EventNotFoundInRepository(str(event.id))
        orm.title = event.title
        orm.description = event.description
        orm.category_id = event.category_id
        orm.season_id = event.season_id
        orm.status = event.status
        orm.opens_at = event.window.opens_at
        orm.closes_at = event.window.closes_at
        orm.resolves_at = event.window.resolves_at
        orm.resolution_source = event.resolution_source
        orm.resolution_criteria = event.resolution_criteria
        orm.outcome = event.outcome
        orm.resolved_at = event.resolved_at
        orm.dispute_window_ends_at = event.dispute_window_ends_at
        orm.updated_at = event.updated_at
        await self._session.flush()
        return orm.to_domain()

    async def list(
        self, criteria: EventFilter, *, include_unlisted: bool = False
    ) -> list[Event]:
        """Выборка по фильтру; сортировка по ``closes_at`` (ближайшие выше).

        ``include_unlisted=False`` (аноним/обычный пользователь) полностью
        скрывает черновики и предложения на модерации — даже при явном фильтре
        ``status=draft``/``proposed`` (защита от IDOR). Редакции передаётся
        ``True`` и статусы видны как есть.
        """
        stmt = select(EventORM)
        if criteria.status is not None:
            stmt = stmt.where(EventORM.status == criteria.status)
        if not include_unlisted:
            stmt = stmt.where(
                EventORM.status.not_in(
                    [EventStatus.DRAFT, EventStatus.PROPOSED]
                )
            )
        if criteria.category_id is not None:
            stmt = stmt.where(EventORM.category_id == criteria.category_id)
        if criteria.season_id is not None:
            stmt = stmt.where(EventORM.season_id == criteria.season_id)
        stmt = (
            # Вторичный ключ ``id`` — детерминированный tie-breaker: при равных
            # ``closes_at`` (частый кейс пакетно созданных событий) страницы не
            # дублируются и не теряют строки между limit/offset-запросами.
            stmt.order_by(EventORM.closes_at.asc(), EventORM.id.asc())
            .limit(criteria.limit)
            .offset(criteria.offset)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]

    async def list_open_due(self, now: datetime) -> Sequence[Event]:
        """Открытые события с истёкшим ``closes_at`` (для авто-закрытия)."""
        stmt = (
            select(EventORM)
            .where(
                EventORM.status == EventStatus.OPEN,
                EventORM.closes_at <= now,
            )
            .order_by(EventORM.closes_at.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]


class SqlAlchemyCategoryRepository:
    """Хранилище категорий поверх асинхронной сессии SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, category_id: uuid.UUID) -> Category | None:
        """Категория по PK."""
        orm = await self._session.get(CategoryORM, category_id)
        return orm.to_domain() if orm else None

    async def get_by_slug(self, slug: str) -> Category | None:
        """Категория по slug (citext — регистронезависимо)."""
        stmt = select(CategoryORM).where(CategoryORM.slug == slug)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def exists(self, category_id: uuid.UUID) -> bool:
        """Существует ли категория с указанным id."""
        return await self._session.get(CategoryORM, category_id) is not None

    async def add(self, category: Category) -> Category:
        """Вставляет категорию, разбирая нарушение ``UNIQUE(slug)``."""
        orm = CategoryORM.from_domain(category)
        self._session.add(orm)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            if "slug" in str(exc.orig):
                raise CategorySlugTakenError(category.slug) from exc
            raise
        return orm.to_domain()

    async def list_all(self) -> list[Category]:
        """Все категории, отсортированные по slug для стабильного вывода."""
        stmt = select(CategoryORM).order_by(CategoryORM.slug.asc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]


class EventNotFoundInRepository(Exception):
    """Внутренняя ошибка адаптера: строка исчезла между чтением и записью."""
