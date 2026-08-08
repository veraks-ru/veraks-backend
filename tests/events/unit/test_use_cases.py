"""Юнит-тесты use-cases events (через порты-фейки).

Покрывают: RBAC (только редакция пишет), проверку существования категории,
сборку/замену окна из патча, идемпотентность правок и переходы статусов.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.events.application.dto import (
    CategoryPatchInput,
    EventPatchInput,
    NewCategoryInput,
    NewEventInput,
)
from app.modules.events.application.use_cases import (
    AnnulEvent,
    CancelEvent,
    CloseEvent,
    CloseExpiredEvents,
    CreateCategory,
    CreateEvent,
    ProposeEvent,
    PublishEvent,
    UpdateCategory,
    UpdateEvent,
)
from app.modules.events.domain.entities import EventStatus
from app.modules.events.domain.errors import (
    CategoryNotFoundError,
    CategorySlugTakenError,
    EventEditNotAllowedError,
    EventNotFoundError,
    EventPermissionError,
    EventSubscriptionRequiredError,
    InvalidEventDataError,
    InvalidEventTransitionError,
    InvalidEventWindowError,
    RestrictedCategoryError,
)
from tests.events.conftest import FIXED_NOW
from tests.events.fakes import (
    FakeAuditTrail,
    FakeClock,
    FakeSubscriptionGate,
    InMemoryCategoryRepository,
    InMemoryEventRepository,
)


@pytest.fixture
def events() -> InMemoryEventRepository:
    return InMemoryEventRepository()


@pytest.fixture
def audit() -> FakeAuditTrail:
    return FakeAuditTrail()


@pytest.fixture
def categories(category, restricted_category) -> InMemoryCategoryRepository:
    repo = InMemoryCategoryRepository()
    repo.seed(category)
    repo.seed(restricted_category)
    return repo


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FIXED_NOW)


def _new_event_input(category_id: uuid.UUID, **over) -> NewEventInput:
    base = {
        "title": "Будет ли X к концу года?",
        "description": "Подробности",
        "category_id": category_id,
        "opens_at": FIXED_NOW + timedelta(days=1),
        "closes_at": FIXED_NOW + timedelta(days=30),
        "resolves_at": FIXED_NOW + timedelta(days=31),
        "resolution_source": "https://source.example",
        "resolution_criteria": "Официальное подтверждение",
    }
    base.update(over)
    return NewEventInput(**base)  # type: ignore[arg-type]


async def test_create_event_as_editor(
    events, categories, clock, audit, editor_actor, category
) -> None:
    uc = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await uc.execute(actor=editor_actor, data=_new_event_input(category.id))
    assert event.status is EventStatus.DRAFT
    assert event.created_by == editor_actor.user_id
    assert await events.get_by_id(event.id) is not None
    # Создание зафиксировано в неизменяемом аудите.
    assert audit.actions() == ["event.created"]
    assert audit.records[0]["entity_id"] == event.id


async def test_create_event_forbidden_for_plain_user(
    events, categories, clock, audit, user_actor, category
) -> None:
    uc = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    with pytest.raises(EventPermissionError):
        await uc.execute(actor=user_actor, data=_new_event_input(category.id))


async def test_create_event_unknown_category(
    events, categories, clock, audit, editor_actor
) -> None:
    uc = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    with pytest.raises(CategoryNotFoundError):
        await uc.execute(actor=editor_actor, data=_new_event_input(uuid.uuid4()))


async def test_create_event_restricted_category_rejected(
    events, categories, clock, audit, editor_actor, restricted_category
) -> None:
    """PRD §7.5: событие в запрещённой категории не создаётся, даже редакцией."""
    uc = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    with pytest.raises(RestrictedCategoryError):
        await uc.execute(
            actor=editor_actor, data=_new_event_input(restricted_category.id)
        )
    assert audit.actions() == []  # отказ до какой-либо записи в аудит


async def test_create_event_invalid_window(
    events, categories, clock, audit, editor_actor, category
) -> None:
    uc = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    bad = _new_event_input(
        category.id,
        opens_at=FIXED_NOW + timedelta(days=30),
        closes_at=FIXED_NOW + timedelta(days=1),  # раньше opens_at
    )
    with pytest.raises(InvalidEventWindowError):
        await uc.execute(actor=editor_actor, data=bad)


async def test_update_event_full_window_replacement(
    events, categories, clock, audit, editor_actor, category
) -> None:
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))

    update = UpdateEvent(events=events, categories=categories, clock=clock, audit=audit)
    patch = EventPatchInput(
        opens_at=FIXED_NOW + timedelta(days=2),
        closes_at=FIXED_NOW + timedelta(days=20),
        resolves_at=FIXED_NOW + timedelta(days=21),
    )
    updated = await update.execute(actor=editor_actor, event_id=event.id, patch=patch)
    assert updated.window.opens_at == FIXED_NOW + timedelta(days=2)


async def test_update_event_partial_window_rejected(
    events, categories, clock, audit, editor_actor, category
) -> None:
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))

    update = UpdateEvent(events=events, categories=categories, clock=clock, audit=audit)
    with pytest.raises(InvalidEventWindowError):
        await update.execute(
            actor=editor_actor,
            event_id=event.id,
            patch=EventPatchInput(closes_at=FIXED_NOW + timedelta(days=5)),
        )


async def test_update_event_move_to_restricted_category_rejected(
    events, categories, clock, audit, editor_actor, category, restricted_category
) -> None:
    """PRD §7.5: PATCH не должен позволять обойти запрет переносом категории."""
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))

    audit.records.clear()
    update = UpdateEvent(events=events, categories=categories, clock=clock, audit=audit)
    with pytest.raises(RestrictedCategoryError):
        await update.execute(
            actor=editor_actor,
            event_id=event.id,
            patch=EventPatchInput(category_id=restricted_category.id),
        )
    # Отказ до сохранения: событие осталось в исходной категории, аудит пуст.
    unchanged = await events.get_by_id(event.id)
    assert unchanged is not None
    assert unchanged.category_id == category.id
    assert audit.actions() == []


async def test_update_event_writes_audit_diff(
    events, categories, clock, audit, editor_actor, category
) -> None:
    """``event.updated`` фиксирует диф «было→стало» только изменённых полей."""
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))

    update = UpdateEvent(events=events, categories=categories, clock=clock, audit=audit)
    await update.execute(
        actor=editor_actor,
        event_id=event.id,
        patch=EventPatchInput(
            title="Новая формулировка", description="Новое описание"
        ),
    )

    record = next(r for r in audit.records if r["action"] == "event.updated")
    assert record["before"] == {"title": event.title, "description": event.description}
    assert record["after"] == {
        "title": "Новая формулировка",
        "description": "Новое описание",
    }
    # Неизменённые поля (категория, окно, источник, критерий) в диф не попали.
    assert "category_id" not in record["before"]
    assert "window" not in record["before"]


async def test_update_event_locks_conditions_after_publish(
    events, categories, clock, audit, editor_actor, category
) -> None:
    """После публикации правка формулировки/критерия/источника запрещена."""
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))
    await PublishEvent(events=events, clock=clock, audit=audit).execute(
        actor=editor_actor, event_id=event.id
    )

    update = UpdateEvent(events=events, categories=categories, clock=clock, audit=audit)
    for patch in (
        EventPatchInput(title="Другая формулировка"),
        EventPatchInput(description="Другое описание"),
        EventPatchInput(resolution_source="https://other.example"),
        EventPatchInput(resolution_criteria="Другой критерий"),
    ):
        with pytest.raises(EventEditNotAllowedError):
            await update.execute(actor=editor_actor, event_id=event.id, patch=patch)


async def test_publish_close_cancel_flow(
    events, categories, clock, audit, editor_actor, category
) -> None:
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))

    publish = PublishEvent(events=events, clock=clock, audit=audit)
    opened = await publish.execute(actor=editor_actor, event_id=event.id)
    assert opened.status is EventStatus.OPEN

    close = CloseEvent(events=events, clock=clock, audit=audit)
    closed = await close.execute(actor=editor_actor, event_id=event.id)
    assert closed.status is EventStatus.CLOSED
    # Каждый переход статуса оставил запись с дифом before→after.
    assert audit.actions() == ["event.created", "event.published", "event.closed"]
    assert audit.records[-1]["before"] == {"status": "open"}
    assert audit.records[-1]["after"] == {"status": "closed"}


async def test_cancel_requires_editor(
    events, categories, clock, audit, editor_actor, user_actor, category
) -> None:
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))

    cancel = CancelEvent(events=events, clock=clock, audit=audit)
    with pytest.raises(EventPermissionError):
        await cancel.execute(actor=user_actor, event_id=event.id)


async def test_close_expired_events_auto_closes_open_past_deadline(
    events, categories, clock, audit, editor_actor, category
) -> None:
    """Фоновое авто-закрытие переводит просроченные open → closed с аудитом."""
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))
    await PublishEvent(events=events, clock=clock, audit=audit).execute(
        actor=editor_actor, event_id=event.id
    )

    # Часы переводим за closes_at (окно: closes_at = FIXED_NOW + 30 дней).
    clock.set(FIXED_NOW + timedelta(days=31))
    closed_ids = await CloseExpiredEvents(
        events=events, clock=clock, audit=audit
    ).execute()

    assert closed_ids == [event.id]
    stored = await events.get_by_id(event.id)
    assert stored is not None and stored.status is EventStatus.CLOSED
    assert "event.closed" in audit.actions()
    # Повторный прогон идемпотентен — закрывать больше нечего.
    assert await CloseExpiredEvents(events=events, clock=clock, audit=audit).execute() == []


async def test_close_expired_events_skips_not_yet_due(
    events, categories, clock, audit, editor_actor, category
) -> None:
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))
    await PublishEvent(events=events, clock=clock, audit=audit).execute(
        actor=editor_actor, event_id=event.id
    )
    # Часы до дедлайна — событие не трогаем.
    closed_ids = await CloseExpiredEvents(
        events=events, clock=clock, audit=audit
    ).execute()
    assert closed_ids == []
    stored = await events.get_by_id(event.id)
    assert stored is not None and stored.status is EventStatus.OPEN


async def _resolved_event(events, categories, clock, audit, editor_actor, category):
    """Готовит в фейковом репозитории событие в статусе ``resolved``."""
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    created = await create.execute(
        actor=editor_actor, data=_new_event_input(category.id)
    )
    event = await events.get_by_id(created.id)
    assert event is not None
    event.publish(now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    event.begin_resolution(now=FIXED_NOW)
    event.record_outcome(
        outcome=True,
        dispute_window_ends_at=FIXED_NOW + timedelta(days=32),
        now=FIXED_NOW,
    )
    return await events.update(event)


async def test_annul_by_arbiter_writes_audit_with_reason(
    events, categories, clock, audit, editor_actor, arbiter_actor, category
) -> None:
    """Арбитр аннулирует разрешённое событие; причина уходит в audit_log."""
    event = await _resolved_event(
        events, categories, clock, audit, editor_actor, category
    )
    uc = AnnulEvent(events=events, clock=clock, audit=audit)

    annulled = await uc.execute(
        actor=arbiter_actor, event_id=event.id, reason="Ошибка источника"
    )

    assert annulled.status is EventStatus.ANNULLED
    stored = await events.get_by_id(event.id)
    assert stored is not None and stored.status is EventStatus.ANNULLED
    assert "event.annulled" in audit.actions()
    record = next(r for r in audit.records if r["action"] == "event.annulled")
    assert record["before"]["status"] == "resolved"
    assert record["after"] == {"status": "annulled", "reason": "Ошибка источника"}
    assert record["actor_id"] == arbiter_actor.user_id


async def test_annul_forbidden_for_editor_and_user(
    events, categories, clock, audit, editor_actor, user_actor, category
) -> None:
    """Редактор ведёт события, но аннулировать их после резолюции не вправе."""
    event = await _resolved_event(
        events, categories, clock, audit, editor_actor, category
    )
    uc = AnnulEvent(events=events, clock=clock, audit=audit)
    for actor in (editor_actor, user_actor):
        with pytest.raises(EventPermissionError):
            await uc.execute(actor=actor, event_id=event.id, reason="Причина")
    stored = await events.get_by_id(event.id)
    assert stored is not None and stored.status is EventStatus.RESOLVED


async def test_annul_requires_non_empty_reason(
    events, categories, clock, audit, editor_actor, arbiter_actor, category
) -> None:
    event = await _resolved_event(
        events, categories, clock, audit, editor_actor, category
    )
    uc = AnnulEvent(events=events, clock=clock, audit=audit)
    with pytest.raises(InvalidEventDataError):
        await uc.execute(actor=arbiter_actor, event_id=event.id, reason="   ")
    stored = await events.get_by_id(event.id)
    assert stored is not None and stored.status is EventStatus.RESOLVED
    assert "event.annulled" not in audit.actions()


async def test_annul_rejected_before_resolution(
    events, categories, clock, audit, editor_actor, arbiter_actor, category
) -> None:
    """Событие до фиксации исхода отменяется (cancel), а не аннулируется."""
    create = CreateEvent(events=events, categories=categories, clock=clock, audit=audit)
    event = await create.execute(actor=editor_actor, data=_new_event_input(category.id))
    uc = AnnulEvent(events=events, clock=clock, audit=audit)
    with pytest.raises(InvalidEventTransitionError):
        await uc.execute(actor=arbiter_actor, event_id=event.id, reason="Рано")


async def test_annul_unknown_event(events, clock, audit, arbiter_actor) -> None:
    uc = AnnulEvent(events=events, clock=clock, audit=audit)
    with pytest.raises(EventNotFoundError):
        await uc.execute(actor=arbiter_actor, event_id=uuid.uuid4(), reason="Причина")


async def test_create_category_slug_conflict(categories, editor_actor, category) -> None:
    uc = CreateCategory(categories=categories)
    with pytest.raises(CategorySlugTakenError):
        await uc.execute(
            actor=editor_actor,
            data=NewCategoryInput(slug=category.slug, title="Дубль"),
        )


async def test_create_category_is_restricted_flag_persisted(categories, editor_actor) -> None:
    uc = CreateCategory(categories=categories)
    created = await uc.execute(
        actor=editor_actor,
        data=NewCategoryInput(slug="new-restricted", title="Новая", is_restricted=True),
    )
    assert created.is_restricted is True


async def test_update_category_edits_title_and_writes_audit(
    categories, audit, editor_actor, category
) -> None:
    uc = UpdateCategory(categories=categories, audit=audit)

    updated = await uc.execute(
        actor=editor_actor,
        category_id=category.id,
        patch=CategoryPatchInput(title="Политика и выборы"),
    )

    assert updated.title == "Политика и выборы"
    assert updated.slug == category.slug  # не тронут
    assert audit.actions() == ["category.updated"]
    # В дифе только изменившееся поле — не весь снимок.
    assert set(audit.records[-1]["after"]) == {"title"}


async def test_update_category_toggles_restricted_flag(
    categories, audit, editor_actor, category
) -> None:
    """Флаг запрета — замена удалению: новые события в категории не создать."""
    uc = UpdateCategory(categories=categories, audit=audit)

    updated = await uc.execute(
        actor=editor_actor,
        category_id=category.id,
        patch=CategoryPatchInput(is_restricted=True),
    )

    assert updated.is_restricted is True


async def test_update_category_without_changes_is_silent_noop(
    categories, audit, editor_actor, category
) -> None:
    uc = UpdateCategory(categories=categories, audit=audit)

    result = await uc.execute(
        actor=editor_actor,
        category_id=category.id,
        patch=CategoryPatchInput(title=category.title),
    )

    assert result.title == category.title
    assert audit.records == []


async def test_update_category_slug_conflict(
    categories, audit, editor_actor, category
) -> None:
    other = await CreateCategory(categories=categories).execute(
        actor=editor_actor, data=NewCategoryInput(slug="sport", title="Спорт")
    )
    uc = UpdateCategory(categories=categories, audit=audit)

    with pytest.raises(CategorySlugTakenError):
        await uc.execute(
            actor=editor_actor,
            category_id=other.id,
            patch=CategoryPatchInput(slug=category.slug),
        )


async def test_update_category_rejects_invalid_slug(
    categories, audit, editor_actor, category
) -> None:
    uc = UpdateCategory(categories=categories, audit=audit)

    with pytest.raises(InvalidEventDataError):
        await uc.execute(
            actor=editor_actor,
            category_id=category.id,
            patch=CategoryPatchInput(slug="Политика Ру"),
        )


async def test_update_category_requires_editor_role(
    categories, audit, user_actor, category
) -> None:
    uc = UpdateCategory(categories=categories, audit=audit)

    with pytest.raises(EventPermissionError):
        await uc.execute(
            actor=user_actor,
            category_id=category.id,
            patch=CategoryPatchInput(title="Взлом"),
        )


async def test_update_unknown_category(categories, audit, editor_actor) -> None:
    uc = UpdateCategory(categories=categories, audit=audit)

    with pytest.raises(CategoryNotFoundError):
        await uc.execute(
            actor=editor_actor,
            category_id=uuid.uuid4(),
            patch=CategoryPatchInput(title="Нет такой"),
        )


async def test_propose_event_as_user_with_subscription(
    events, categories, clock, audit, user_actor, category
) -> None:
    uc = ProposeEvent(
        events=events,
        categories=categories,
        clock=clock,
        audit=audit,
        subscriptions=FakeSubscriptionGate(active=True),
    )
    event = await uc.execute(actor=user_actor, data=_new_event_input(category.id))
    assert event.status is EventStatus.PROPOSED
    assert audit.actions() == ["event.proposed"]


async def test_propose_event_requires_subscription(
    events, categories, clock, audit, user_actor, category
) -> None:
    uc = ProposeEvent(
        events=events,
        categories=categories,
        clock=clock,
        audit=audit,
        subscriptions=FakeSubscriptionGate(active=False),
    )
    with pytest.raises(EventSubscriptionRequiredError):
        await uc.execute(actor=user_actor, data=_new_event_input(category.id))


async def test_propose_event_restricted_category_rejected(
    events, categories, clock, audit, user_actor, restricted_category
) -> None:
    """PRD §7.5: пользователь не может предложить событие в запрещённой категории."""
    uc = ProposeEvent(
        events=events,
        categories=categories,
        clock=clock,
        audit=audit,
        subscriptions=FakeSubscriptionGate(active=True),
    )
    with pytest.raises(RestrictedCategoryError):
        await uc.execute(
            actor=user_actor, data=_new_event_input(restricted_category.id)
        )
    assert audit.actions() == []
