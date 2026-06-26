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
from app.modules.identity.domain.entities import UserRole
from app.shared.audit.domain.entities import AuditActorType
from app.shared.audit.ports.audit_trail import AuditTrail

_ACTOR_TYPE_BY_ROLE: dict[UserRole, AuditActorType] = {
    UserRole.USER: AuditActorType.USER,
    UserRole.EDITOR: AuditActorType.EDITOR,
    UserRole.ARBITER: AuditActorType.ARBITER,
    UserRole.ADMIN: AuditActorType.ADMIN,
}


def _actor_type(role: UserRole) -> AuditActorType:
    """Маппит RBAC-роль в тип актора аудита."""
    return _ACTOR_TYPE_BY_ROLE.get(role, AuditActorType.USER)


def _status_value(event: Event) -> str:
    """Строковое значение статуса события для снимков аудита."""
    return event.status.value


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
        self,
        *,
        events: EventRepository,
        categories: CategoryRepository,
        clock: Clock,
        audit: AuditTrail,
    ) -> None:
        self._events = events
        self._categories = categories
        self._clock = clock
        self._audit = audit

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
        saved = await self._events.add(event)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="event.created",
            entity_type="event",
            entity_id=saved.id,
            after={
                "title": saved.title,
                "status": _status_value(saved),
                "category_id": str(saved.category_id),
            },
        )
        return saved


class UpdateEvent:
    """Частичное редактирование события (до закрытия приёма)."""

    def __init__(
        self,
        *,
        events: EventRepository,
        categories: CategoryRepository,
        clock: Clock,
        audit: AuditTrail,
    ) -> None:
        self._events = events
        self._categories = categories
        self._clock = clock
        self._audit = audit

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
            saved = await self._events.update(event)
            await self._audit.record(
                actor_id=actor.user_id,
                actor_type=_actor_type(actor.role),
                action="event.updated",
                entity_type="event",
                entity_id=saved.id,
                after={
                    "title": saved.title,
                    "category_id": str(saved.category_id),
                    "status": _status_value(saved),
                },
            )
            return saved
        return event

    async def _load(self, event_id: uuid.UUID) -> Event:
        event = await self._events.get_by_id(event_id)
        if event is None:
            raise EventNotFoundError("Событие не найдено")
        return event


class _TransitionUseCase:
    """База для переходов статуса с аудитом ``before → after``.

    Подклассы задают ``_action`` и применяют переход доменным методом в
    ``_apply``. Запись в неизменяемый журнал — общая (диф статуса).
    """

    _action: str

    def __init__(
        self, *, events: EventRepository, clock: Clock, audit: AuditTrail
    ) -> None:
        self._events = events
        self._clock = clock
        self._audit = audit

    def _apply(self, event: Event) -> None:  # pragma: no cover - переопределяется
        raise NotImplementedError

    async def execute(self, *, actor: Actor, event_id: uuid.UUID) -> Event:
        """Проверяет права, применяет переход и пишет запись аудита."""
        ensure_can_manage_events(actor.role)
        event = await _require_event(self._events, event_id)
        before_status = _status_value(event)
        self._apply(event)
        saved = await self._events.update(event)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action=self._action,
            entity_type="event",
            entity_id=saved.id,
            before={"status": before_status},
            after={"status": _status_value(saved)},
        )
        return saved


class PublishEvent(_TransitionUseCase):
    """Переход ``draft → open`` (открытие приёма прогнозов)."""

    _action = "event.published"

    def _apply(self, event: Event) -> None:
        event.publish(now=self._clock.now())


class CloseEvent(_TransitionUseCase):
    """Переход ``open → closed`` (блокировка приёма прогнозов)."""

    _action = "event.closed"

    def _apply(self, event: Event) -> None:
        event.close(now=self._clock.now())


class CancelEvent(_TransitionUseCase):
    """Переход в ``cancelled`` (отмена события редакцией)."""

    _action = "event.cancelled"

    def _apply(self, event: Event) -> None:
        event.cancel(now=self._clock.now())


class CloseExpiredEvents:
    """Авто-закрытие приёма по истёкшему ``closes_at`` (системный триггер).

    Фоновая задача: переводит ``open → closed`` все события, чей серверный
    дедлайн прошёл, и пишет системную запись аудита. Возвращает id закрытых
    событий — вызывающий (воркер) по ним блокирует прогнозы (домен predictions).
    Идемпотентна: повторный прогон не находит уже закрытых.
    """

    def __init__(
        self, *, events: EventRepository, clock: Clock, audit: AuditTrail
    ) -> None:
        self._events = events
        self._clock = clock
        self._audit = audit

    async def execute(self) -> list[uuid.UUID]:
        """Закрывает все просроченные открытые события; отдаёт их id."""
        now = self._clock.now()
        closed: list[uuid.UUID] = []
        for event in await self._events.list_open_due(now):
            event.close(now=now)
            saved = await self._events.update(event)
            await self._audit.record(
                actor_id=None,
                actor_type=AuditActorType.SYSTEM,
                action="event.closed",
                entity_type="event",
                entity_id=saved.id,
                before={"status": "open"},
                after={"status": _status_value(saved)},
                metadata={"reason": "auto_close_deadline"},
            )
            closed.append(saved.id)
        return closed


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
