"""Use-cases домена events.

Каждый класс — одна бизнес-операция. Зависимости передаются только через
порты (конструктор), поэтому use-cases изолированы от FastAPI, SQLAlchemy и
часов реального времени и покрываются юнит-тестами с фейками.

Операции записи (создание/правка/переходы) требуют роль редактора —
проверка вынесена в доменную политику ``ensure_can_manage_events``. Чтения
(детали, список, категории) публичны.
"""

from __future__ import annotations

import uuid

from app.modules.events.application.dto import (
    Actor,
    EventPatchInput,
    NewCategoryInput,
    NewEventInput,
)
from app.modules.events.domain.entities import Category, Event
from app.modules.events.domain.errors import (
    CategoryNotFoundError,
    EventNotFoundError,
    InvalidEventWindowError,
)
from app.modules.events.domain.policies import ensure_can_manage_events
from app.modules.events.domain.value_objects import EventWindow
from app.modules.events.ports.clock import Clock
from app.modules.events.ports.repositories import (
    CategoryRepository,
    EventFilter,
    EventRepository,
)


def _window_from_patch(patch: EventPatchInput) -> EventWindow | None:
    """Собирает окно из патча: либо все три отметки, либо ни одной.

    Частичная замена окна нарушила бы инвариант порядка дат, поэтому
    допускается только целостная замена.
    """
    parts = (patch.opens_at, patch.closes_at, patch.resolves_at)
    provided = [p for p in parts if p is not None]
    if not provided:
        return None
    if len(provided) != 3:
        raise InvalidEventWindowError(
            "Окно меняется целиком: укажите opens_at, closes_at и resolves_at вместе"
        )
    assert patch.opens_at and patch.closes_at and patch.resolves_at
    return EventWindow(
        opens_at=patch.opens_at,
        closes_at=patch.closes_at,
        resolves_at=patch.resolves_at,
    )


class CreateEvent:
    """Создание черновика события редакцией."""

    def __init__(
        self, *, events: EventRepository, categories: CategoryRepository, clock: Clock
    ) -> None:
        self._events = events
        self._categories = categories
        self._clock = clock

    async def execute(self, *, actor: Actor, data: NewEventInput) -> Event:
        """Проверяет права и категорию, валидирует окно и сохраняет черновик."""
        ensure_can_manage_events(actor.role)
        if not await self._categories.exists(data.category_id):
            raise CategoryNotFoundError("Указанная категория не существует")

        window = EventWindow(
            opens_at=data.opens_at,
            closes_at=data.closes_at,
            resolves_at=data.resolves_at,
        )
        event = Event.create_draft(
            title=data.title,
            description=data.description,
            category_id=data.category_id,
            created_by=actor.user_id,
            window=window,
            resolution_source=data.resolution_source,
            resolution_criteria=data.resolution_criteria,
            season_id=data.season_id,
            now=self._clock.now(),
        )
        return await self._events.add(event)


class UpdateEvent:
    """Частичное редактирование события (до закрытия приёма)."""

    def __init__(
        self, *, events: EventRepository, categories: CategoryRepository, clock: Clock
    ) -> None:
        self._events = events
        self._categories = categories
        self._clock = clock

    async def execute(
        self, *, actor: Actor, event_id: uuid.UUID, patch: EventPatchInput
    ) -> Event:
        """Применяет правки с учётом статуса и прав; сохраняет при изменениях."""
        ensure_can_manage_events(actor.role)
        event = await self._load(event_id)
        if patch.category_id is not None and not await self._categories.exists(
            patch.category_id
        ):
            raise CategoryNotFoundError("Указанная категория не существует")

        changed = event.apply_edits(
            title=patch.title,
            description=patch.description,
            category_id=patch.category_id,
            season_id=patch.season_id,
            window=_window_from_patch(patch),
            resolution_source=patch.resolution_source,
            resolution_criteria=patch.resolution_criteria,
            now=self._clock.now(),
        )
        if changed:
            return await self._events.update(event)
        return event

    async def _load(self, event_id: uuid.UUID) -> Event:
        event = await self._events.get_by_id(event_id)
        if event is None:
            raise EventNotFoundError("Событие не найдено")
        return event


class PublishEvent:
    """Переход ``draft → open`` (открытие приёма прогнозов)."""

    def __init__(self, *, events: EventRepository, clock: Clock) -> None:
        self._events = events
        self._clock = clock

    async def execute(self, *, actor: Actor, event_id: uuid.UUID) -> Event:
        """Публикует черновик после проверки прав и актуальности окна."""
        ensure_can_manage_events(actor.role)
        event = await _require_event(self._events, event_id)
        event.publish(now=self._clock.now())
        return await self._events.update(event)


class CloseEvent:
    """Переход ``open → closed`` (блокировка приёма прогнозов)."""

    def __init__(self, *, events: EventRepository, clock: Clock) -> None:
        self._events = events
        self._clock = clock

    async def execute(self, *, actor: Actor, event_id: uuid.UUID) -> Event:
        """Закрывает приём прогнозов вручную (editor/admin)."""
        ensure_can_manage_events(actor.role)
        event = await _require_event(self._events, event_id)
        event.close(now=self._clock.now())
        return await self._events.update(event)


class CancelEvent:
    """Переход в ``cancelled`` (отмена события редакцией)."""

    def __init__(self, *, events: EventRepository, clock: Clock) -> None:
        self._events = events
        self._clock = clock

    async def execute(self, *, actor: Actor, event_id: uuid.UUID) -> Event:
        """Отменяет событие (из draft/open/closed)."""
        ensure_can_manage_events(actor.role)
        event = await _require_event(self._events, event_id)
        event.cancel(now=self._clock.now())
        return await self._events.update(event)


class GetEvent:
    """Чтение деталей события (публично)."""

    def __init__(self, *, events: EventRepository) -> None:
        self._events = events

    async def execute(self, *, event_id: uuid.UUID) -> Event:
        """Возвращает событие или поднимает :class:`EventNotFoundError`."""
        return await _require_event(self._events, event_id)


class ListEvents:
    """Чтение каталога событий по фильтрам (публично)."""

    def __init__(self, *, events: EventRepository) -> None:
        self._events = events

    async def execute(self, *, criteria: EventFilter) -> list[Event]:
        """Возвращает страницу событий, отсортированных по ``closes_at``."""
        return await self._events.list(criteria)


class CreateCategory:
    """Создание категории (editor/admin)."""

    def __init__(self, *, categories: CategoryRepository) -> None:
        self._categories = categories

    async def execute(self, *, actor: Actor, data: NewCategoryInput) -> Category:
        """Проверяет права, существование родителя и создаёт категорию."""
        ensure_can_manage_events(actor.role)
        if data.parent_id is not None and not await self._categories.exists(
            data.parent_id
        ):
            raise CategoryNotFoundError("Родительская категория не существует")
        category = Category.create(
            slug=data.slug,
            title=data.title,
            description=data.description,
            parent_id=data.parent_id,
        )
        return await self._categories.add(category)


class ListCategories:
    """Чтение дерева категорий (публично)."""

    def __init__(self, *, categories: CategoryRepository) -> None:
        self._categories = categories

    async def execute(self) -> list[Category]:
        """Возвращает плоский список категорий (дерево собирается на клиенте/SSR)."""
        return await self._categories.list_all()


async def _require_event(events: EventRepository, event_id: uuid.UUID) -> Event:
    """Загружает событие или поднимает :class:`EventNotFoundError`."""
    event = await events.get_by_id(event_id)
    if event is None:
        raise EventNotFoundError("Событие не найдено")
    return event
