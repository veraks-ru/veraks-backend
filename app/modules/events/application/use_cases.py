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
    CategoryPatchInput,
    EventPatchInput,
    NewCategoryInput,
    NewEventInput,
)
from app.modules.events.domain.entities import Category, Event, EventStatus
from app.modules.events.domain.errors import (
    CategoryNotFoundError,
    EventNotFoundError,
    EventSubscriptionRequiredError,
    InvalidEventWindowError,
    RestrictedCategoryError,
)
from app.modules.events.domain.policies import (
    ensure_can_annul_event,
    ensure_can_manage_events,
)
from app.modules.events.domain.value_objects import EventWindow
from app.modules.events.ports.clock import Clock
from app.modules.events.ports.notifications import Notifier
from app.modules.events.ports.repositories import (
    CategoryRepository,
    EventFilter,
    EventRepository,
)
from app.modules.events.ports.subscriptions import SubscriptionGate
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


def _window_snapshot(window: EventWindow) -> dict[str, str]:
    """JSON-сериализуемый снимок временного окна для аудита."""
    return {
        "opens_at": window.opens_at.isoformat(),
        "closes_at": window.closes_at.isoformat(),
        "resolves_at": window.resolves_at.isoformat(),
    }


def _event_snapshot(event: Event) -> dict[str, object]:
    """Снимок редактируемых полей события для дифа аудита ``event.updated``."""
    return {
        "title": event.title,
        "description": event.description,
        "category_id": str(event.category_id),
        "season_id": str(event.season_id) if event.season_id else None,
        "window": _window_snapshot(event.window),
        "resolution_source": event.resolution_source,
        "resolution_criteria": event.resolution_criteria,
    }


def _category_snapshot(category: Category) -> dict[str, object]:
    """Снимок редактируемых полей категории для дифа ``category.updated``."""
    return {
        "slug": category.slug,
        "title": category.title,
        "description": category.description,
        "is_restricted": category.is_restricted,
    }


def _diff_snapshots(
    before: dict[str, object], after: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]]:
    """Оставляет в дифе только реально изменившиеся поля («было» / «стало»)."""
    changed_before: dict[str, object] = {}
    changed_after: dict[str, object] = {}
    for field_name, old_value in before.items():
        new_value = after[field_name]
        if new_value != old_value:
            changed_before[field_name] = old_value
            changed_after[field_name] = new_value
    return changed_before, changed_after


async def _ensure_category_allowed(
    categories: CategoryRepository, category_id: uuid.UUID
) -> None:
    """Проверяет, что категория существует и не запрещена (PRD §7.5).

    Общая проверка для :class:`CreateEvent` и :class:`ProposeEvent`: событие
    не может быть создано/предложено в категории с ``is_restricted=true``.
    """
    category = await categories.get_by_id(category_id)
    if category is None:
        raise CategoryNotFoundError("Указанная категория не существует")
    if category.is_restricted:
        raise RestrictedCategoryError(
            f"Категория «{category.title}» запрещена для событий по правилам платформы"
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
        await _ensure_category_allowed(self._categories, data.category_id)

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


class ProposeEvent:
    """Предложение события пользователем (статус ``proposed``, на модерацию).

    Роль не проверяется (предлагать может любой участник), но нужна активная
    подписка — как и для голосования. Валидация окна/категории/текста — та же,
    что у :class:`CreateEvent`.
    """

    def __init__(
        self,
        *,
        events: EventRepository,
        categories: CategoryRepository,
        clock: Clock,
        audit: AuditTrail,
        subscriptions: SubscriptionGate,
    ) -> None:
        self._events = events
        self._categories = categories
        self._clock = clock
        self._audit = audit
        self._subscriptions = subscriptions

    async def execute(self, *, actor: Actor, data: NewEventInput) -> Event:
        """Проверяет подписку и данные, сохраняет предложение на модерацию."""
        now = self._clock.now()
        if not await self._subscriptions.has_active_subscription(actor.user_id, now):
            raise EventSubscriptionRequiredError(
                "Предлагать события можно только с активной подпиской"
            )
        await _ensure_category_allowed(self._categories, data.category_id)

        window = EventWindow(
            opens_at=data.opens_at,
            closes_at=data.closes_at,
            resolves_at=data.resolves_at,
        )
        event = Event.create_proposed(
            title=data.title,
            description=data.description,
            category_id=data.category_id,
            created_by=actor.user_id,
            window=window,
            resolution_source=data.resolution_source,
            resolution_criteria=data.resolution_criteria,
            season_id=data.season_id,
            now=now,
        )
        saved = await self._events.add(event)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="event.proposed",
            entity_type="event",
            entity_id=saved.id,
            after={"title": saved.title, "status": _status_value(saved)},
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
        if patch.category_id is not None:
            # Та же проверка, что при создании/предложении: переносить
            # событие в запрещённую категорию (PRD §7.5) правкой нельзя —
            # иначе это был бы обход запрета через PATCH.
            await _ensure_category_allowed(self._categories, patch.category_id)

        before_snapshot = _event_snapshot(event)
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
            diff_before, diff_after = _diff_snapshots(
                before_snapshot, _event_snapshot(saved)
            )
            await self._audit.record(
                actor_id=actor.user_id,
                actor_type=_actor_type(actor.role),
                action="event.updated",
                entity_type="event",
                entity_id=saved.id,
                before=diff_before,
                after=diff_after,
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


class AnnulEvent:
    """Аннулирование события после резолюции: ``resolved|disputed → annulled``.

    Организатор конкурса вправе признать событие некорректным уже после
    фиксации исхода (ст. 1058 ГК РФ, PRD §7.5/§4.8): двусмысленная
    формулировка, ошибка источника, неразрешимый спор. Аннулированное событие
    исключается из всех рейтингов и калибровки (выборки скоринга идут по
    статусу ``resolved``).

    Отличия от переходов семейства :class:`_TransitionUseCase`: своя роль
    (арбитр/админ, см. ``ensure_can_annul_event``) и обязательная причина,
    которая уходит в неизменяемый ``audit_log`` действием ``event.annulled``.
    Существующие строки ``resolutions`` не правятся — журнал решений
    append-only, аннулирование фиксируется только аудитом.

    Пересчёт затронутых срезов рейтинга и снятие открытых споров события —
    забота вызывающего (композит-рут: роутер events так же, как воркер после
    ``score_event``).
    """

    def __init__(
        self, *, events: EventRepository, clock: Clock, audit: AuditTrail
    ) -> None:
        self._events = events
        self._clock = clock
        self._audit = audit

    async def execute(
        self, *, actor: Actor, event_id: uuid.UUID, reason: str
    ) -> Event:
        """Проверяет права и причину, аннулирует событие и пишет аудит.

        Строка события читается ``FOR UPDATE``: иначе гонка «аннулирование ↔
        подача спора» могла бы оставить открытый спор у уже аннулированного
        события — тот самый тупик, который снимает ``VoidEventDisputes``.
        """
        ensure_can_annul_event(actor.role)
        event = await _require_event(self._events, event_id, for_update=True)
        before = _status_value(event)
        reason_text = event.annul(reason=reason, now=self._clock.now())
        saved = await self._events.update(event)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="event.annulled",
            entity_type="event",
            entity_id=saved.id,
            before={"status": before, "outcome": saved.outcome},
            after={"status": _status_value(saved), "reason": reason_text},
        )
        return saved


class ApproveEvent:
    """Модерация: ``proposed → draft`` (editor/admin) + уведомление автору."""

    def __init__(
        self,
        *,
        events: EventRepository,
        clock: Clock,
        audit: AuditTrail,
        notifier: Notifier,
    ) -> None:
        self._events = events
        self._clock = clock
        self._audit = audit
        self._notifier = notifier

    async def execute(self, *, actor: Actor, event_id: uuid.UUID) -> Event:
        ensure_can_manage_events(actor.role)
        event = await _require_event(self._events, event_id)
        before = _status_value(event)
        author, title = event.created_by, event.title
        event.approve(now=self._clock.now())
        saved = await self._events.update(event)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="event.approved",
            entity_type="event",
            entity_id=saved.id,
            before={"status": before},
            after={"status": _status_value(saved)},
        )
        await self._notifier.emit(
            user_id=author,
            kind="event.approved",
            title="Событие одобрено",
            body=f"«{title}» одобрено модерацией и будет опубликовано.",
            entity_type="event",
            entity_id=saved.id,
        )
        return saved


class RejectEvent:
    """Модерация: ``proposed → cancelled`` (editor/admin) + причина автору."""

    def __init__(
        self,
        *,
        events: EventRepository,
        clock: Clock,
        audit: AuditTrail,
        notifier: Notifier,
    ) -> None:
        self._events = events
        self._clock = clock
        self._audit = audit
        self._notifier = notifier

    async def execute(self, *, actor: Actor, event_id: uuid.UUID, reason: str) -> Event:
        ensure_can_manage_events(actor.role)
        event = await _require_event(self._events, event_id)
        before = _status_value(event)
        author = event.created_by
        event.reject(now=self._clock.now())
        saved = await self._events.update(event)
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="event.rejected",
            entity_type="event",
            entity_id=saved.id,
            before={"status": before},
            after={"status": _status_value(saved), "reason": reason},
        )
        await self._notifier.emit(
            user_id=author,
            kind="event.rejected",
            title="Событие отклонено",
            body=reason.strip() or "Предложение отклонено модерацией.",
            entity_type="event",
            entity_id=saved.id,
        )
        return saved


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


_UNLISTED_STATUSES = frozenset({EventStatus.DRAFT, EventStatus.PROPOSED})


def _can_see_unlisted(viewer: Actor | None) -> bool:
    """Может ли зритель видеть черновики/предложения (редакция), кроме своих."""
    return viewer is not None and viewer.role in {
        UserRole.EDITOR,
        UserRole.ARBITER,
        UserRole.ADMIN,
    }


class GetEvent:
    """Чтение деталей события (публично; черновики/предложения — ограничены)."""

    def __init__(self, *, events: EventRepository) -> None:
        self._events = events

    async def execute(
        self, *, event_id: uuid.UUID, viewer: Actor | None = None
    ) -> Event:
        """Возвращает событие или :class:`EventNotFoundError`.

        Черновик/предложение на модерации видны только редакции и автору; всем
        остальным (включая анонимов) отдаём 404, не раскрывая факт существования.
        """
        event = await _require_event(self._events, event_id)
        if event.status in _UNLISTED_STATUSES and not _can_see_unlisted(viewer):
            is_author = viewer is not None and viewer.user_id == event.created_by
            if not is_author:
                raise EventNotFoundError(str(event_id))
        return event


class ListEvents:
    """Чтение каталога событий по фильтрам (публично; unlisted — только редакции)."""

    def __init__(self, *, events: EventRepository) -> None:
        self._events = events

    async def execute(
        self, *, criteria: EventFilter, viewer: Actor | None = None
    ) -> list[Event]:
        """Страница событий по ``closes_at``.

        Непривилегированному зрителю черновики и предложения на модерации не
        отдаются ни при каком фильтре статуса (защита от IDOR-раскрытия).
        """
        return await self._events.list(
            criteria, include_unlisted=_can_see_unlisted(viewer)
        )


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
            is_restricted=data.is_restricted,
        )
        return await self._categories.add(category)


class UpdateCategory:
    """Частичное редактирование категории (editor/admin).

    Нужна прежде всего для исправления опечаток в названии и для смены флага
    запрещённой тематики. Категория участвует в пороге разнообразия ``C_MIN``,
    поэтому переименование не влияет на зачёт — меняется только подпись.
    """

    def __init__(
        self, *, categories: CategoryRepository, audit: AuditTrail
    ) -> None:
        self._categories = categories
        self._audit = audit

    async def execute(
        self, *, actor: Actor, category_id: uuid.UUID, patch: CategoryPatchInput
    ) -> Category:
        """Применяет правки и пишет аудит; без изменений — тихий no-op."""
        ensure_can_manage_events(actor.role)
        category = await self._categories.get_by_id(category_id)
        if category is None:
            raise CategoryNotFoundError("Категория не найдена")

        before = _category_snapshot(category)
        changed = category.apply_edits(
            slug=patch.slug,
            title=patch.title,
            description=patch.description,
            is_restricted=patch.is_restricted,
        )
        if not changed:
            return category

        saved = await self._categories.update(category)
        diff_before, diff_after = _diff_snapshots(before, _category_snapshot(saved))
        await self._audit.record(
            actor_id=actor.user_id,
            actor_type=_actor_type(actor.role),
            action="category.updated",
            entity_type="category",
            entity_id=saved.id,
            before=diff_before,
            after=diff_after,
        )
        return saved


class ListCategories:
    """Чтение дерева категорий (публично)."""

    def __init__(self, *, categories: CategoryRepository) -> None:
        self._categories = categories

    async def execute(self) -> list[Category]:
        """Возвращает плоский список категорий (дерево собирается на клиенте/SSR)."""
        return await self._categories.list_all()


async def _require_event(
    events: EventRepository, event_id: uuid.UUID, *, for_update: bool = False
) -> Event:
    """Загружает событие или поднимает :class:`EventNotFoundError`."""
    event = await events.get_by_id(event_id, for_update=for_update)
    if event is None:
        raise EventNotFoundError("Событие не найдено")
    return event
