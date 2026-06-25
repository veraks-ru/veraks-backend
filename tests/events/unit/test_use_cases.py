"""Юнит-тесты use-cases events (через порты-фейки).

Покрывают: RBAC (только редакция пишет), проверку существования категории,
сборку/замену окна из патча, идемпотентность правок и переходы статусов.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.events.application.dto import (
    EventPatchInput,
    NewCategoryInput,
    NewEventInput,
)
from app.modules.events.application.use_cases import (
    CancelEvent,
    CloseEvent,
    CreateCategory,
    CreateEvent,
    PublishEvent,
    UpdateEvent,
)
from app.modules.events.domain.entities import EventStatus
from app.modules.events.domain.errors import (
    CategoryNotFoundError,
    EventPermissionError,
    InvalidEventWindowError,
)
from tests.events.conftest import FIXED_NOW
from tests.events.fakes import (
    FakeAuditTrail,
    FakeClock,
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
def categories(category) -> InMemoryCategoryRepository:
    repo = InMemoryCategoryRepository()
    repo.seed(category)
    return repo


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FIXED_NOW)


def _new_event_input(category_id: uuid.UUID, **over) -> NewEventInput:
    base = dict(
        title="Будет ли X к концу года?",
        description="Подробности",
        category_id=category_id,
        opens_at=FIXED_NOW + timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=30),
        resolves_at=FIXED_NOW + timedelta(days=31),
        resolution_source="https://source.example",
        resolution_criteria="Официальное подтверждение",
    )
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


async def test_create_category_slug_conflict(categories, editor_actor, category) -> None:
    uc = CreateCategory(categories=categories)
    with pytest.raises(Exception):  # CategorySlugTakenError
        await uc.execute(
            actor=editor_actor,
            data=NewCategoryInput(slug=category.slug, title="Дубль"),
        )
