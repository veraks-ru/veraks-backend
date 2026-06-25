"""Юнит-тесты доменной сущности ``Event`` — конечный автомат статусов.

Покрывают ядро домена: разрешённые/запрещённые переходы жизненного цикла,
правила редактирования по статусам и фиксацию полей после публикации.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.events.domain.entities import Event, EventStatus
from app.modules.events.domain.errors import (
    EventEditNotAllowedError,
    InvalidEventTransitionError,
)
from app.modules.events.domain.value_objects import EventWindow
from tests.events.conftest import FIXED_NOW


def _make_draft(window: EventWindow) -> Event:
    return Event.create_draft(
        title="Будет ли X?",
        description="Описание события",
        category_id=uuid.uuid4(),
        created_by=uuid.uuid4(),
        window=window,
        resolution_source="https://source.example/x",
        resolution_criteria="Засчитывается при официальном подтверждении",
        now=FIXED_NOW,
    )


def test_create_draft_starts_in_draft(future_window) -> None:
    event = _make_draft(future_window)
    assert event.status is EventStatus.DRAFT
    assert event.outcome is None


def test_publish_opens_event(future_window) -> None:
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    assert event.status is EventStatus.OPEN
    assert event.can_accept_predictions(now=future_window.opens_at)


def test_publish_rejected_when_window_expired(future_window) -> None:
    event = _make_draft(future_window)
    too_late = future_window.closes_at + timedelta(seconds=1)
    with pytest.raises(InvalidEventTransitionError):
        event.publish(now=too_late)


def test_full_happy_path_transitions(future_window) -> None:
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    assert event.status is EventStatus.CLOSED
    event.begin_resolution(now=FIXED_NOW)
    assert event.status is EventStatus.RESOLVING


def test_cannot_close_a_draft(future_window) -> None:
    event = _make_draft(future_window)
    with pytest.raises(InvalidEventTransitionError):
        event.close(now=FIXED_NOW)


def test_cancel_from_open(future_window) -> None:
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    event.cancel(now=FIXED_NOW)
    assert event.status is EventStatus.CANCELLED


def test_cancelled_is_terminal(future_window) -> None:
    event = _make_draft(future_window)
    event.cancel(now=FIXED_NOW)
    with pytest.raises(InvalidEventTransitionError):
        event.publish(now=FIXED_NOW)


def test_edit_draft_changes_all_fields(future_window) -> None:
    event = _make_draft(future_window)
    new_window = EventWindow(
        opens_at=future_window.opens_at + timedelta(days=1),
        closes_at=future_window.closes_at + timedelta(days=1),
        resolves_at=future_window.resolves_at + timedelta(days=1),
    )
    changed = event.apply_edits(
        title="Новый заголовок", window=new_window, now=FIXED_NOW
    )
    assert changed is True
    assert event.title == "Новый заголовок"
    assert event.window == new_window


def test_edit_open_event_locks_window_and_category(future_window) -> None:
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    # Заголовок/критерии правятся.
    assert event.apply_edits(title="Уточнение", now=FIXED_NOW) is True
    # Категория заблокирована после публикации.
    with pytest.raises(EventEditNotAllowedError):
        event.apply_edits(category_id=uuid.uuid4(), now=FIXED_NOW)
    # Окно заблокировано после публикации.
    moved = EventWindow(
        opens_at=future_window.opens_at,
        closes_at=future_window.closes_at + timedelta(days=2),
        resolves_at=future_window.resolves_at + timedelta(days=2),
    )
    with pytest.raises(EventEditNotAllowedError):
        event.apply_edits(window=moved, now=FIXED_NOW)


def test_edit_forbidden_after_close(future_window) -> None:
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    with pytest.raises(EventEditNotAllowedError):
        event.apply_edits(title="Поздно", now=FIXED_NOW)


def test_edit_noop_returns_false(future_window) -> None:
    event = _make_draft(future_window)
    assert event.apply_edits(title=event.title, now=FIXED_NOW) is False
