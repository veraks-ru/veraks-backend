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
    InvalidEventDataError,
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


def _make_resolved(window: EventWindow) -> Event:
    """Событие, доведённое до ``resolved`` (предусловие аннулирования)."""
    event = _make_draft(window)
    event.publish(now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    event.begin_resolution(now=FIXED_NOW)
    event.record_outcome(
        outcome=True, dispute_window_ends_at=window.resolves_at, now=FIXED_NOW
    )
    return event


def test_annul_from_resolved(future_window) -> None:
    """``resolved → annulled``; исход остаётся в истории, статус — терминальный."""
    event = _make_resolved(future_window)
    reason = event.annul(reason="  Двусмысленная формулировка  ", now=FIXED_NOW)
    assert event.status is EventStatus.ANNULLED
    assert reason == "Двусмысленная формулировка"  # нормализованная причина
    assert event.outcome is True  # денормализованный исход не стирается
    with pytest.raises(InvalidEventTransitionError):
        event.open_dispute(now=FIXED_NOW)


def test_annul_from_disputed(future_window) -> None:
    """Неразрешимый спор тоже заканчивается аннулированием."""
    event = _make_resolved(future_window)
    event.open_dispute(now=FIXED_NOW)
    event.annul(reason="Спор неразрешим", now=FIXED_NOW)
    assert event.status is EventStatus.ANNULLED


@pytest.mark.parametrize("reason", ["", "   "])
def test_annul_requires_reason(future_window, reason: str) -> None:
    """Пустая причина запрещена и НЕ меняет состояние события."""
    event = _make_resolved(future_window)
    with pytest.raises(InvalidEventDataError):
        event.annul(reason=reason, now=FIXED_NOW)
    assert event.status is EventStatus.RESOLVED


def test_annul_forbidden_before_resolution(future_window) -> None:
    """До фиксации исхода аннулирования нет — есть отмена (``cancelled``)."""
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    with pytest.raises(InvalidEventTransitionError):
        event.annul(reason="Рано", now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    with pytest.raises(InvalidEventTransitionError):
        event.annul(reason="Всё ещё рано", now=FIXED_NOW)


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
    # Сезон заблокирован после публикации (честность сезонного зачёта).
    with pytest.raises(EventEditNotAllowedError):
        event.apply_edits(season_id=uuid.uuid4(), now=FIXED_NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Новая формулировка"),
        ("description", "Новое описание"),
        ("resolution_source", "https://other-source.example"),
        ("resolution_criteria", "Другой критерий засчитывания"),
    ],
)
def test_edit_open_event_locks_condition_fields(
    future_window, field: str, value: str
) -> None:
    """Ст. 1058 ГК РФ: условия конкурса после публикации не меняются."""
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    with pytest.raises(EventEditNotAllowedError):
        event.apply_edits(now=FIXED_NOW, **{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Новая формулировка"),
        ("description", "Новое описание"),
        ("resolution_source", "https://other-source.example"),
        ("resolution_criteria", "Другой критерий засчитывания"),
    ],
)
def test_edit_draft_allows_condition_fields(
    future_window, field: str, value: str
) -> None:
    """В ``draft`` (до публикации) условия конкурса ещё свободно правятся."""
    event = _make_draft(future_window)
    changed = event.apply_edits(now=FIXED_NOW, **{field: value})
    assert changed is True
    assert getattr(event, field) == value


def test_edit_open_event_allows_noop_condition_fields(future_window) -> None:
    """Повтор того же значения после публикации — не правка, а no-op."""
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    assert event.apply_edits(title=event.title, now=FIXED_NOW) is False


def test_edit_draft_allows_season_change(future_window) -> None:
    event = _make_draft(future_window)
    season_id = uuid.uuid4()
    assert event.apply_edits(season_id=season_id, now=FIXED_NOW) is True
    assert event.season_id == season_id


def test_edit_forbidden_after_close(future_window) -> None:
    event = _make_draft(future_window)
    event.publish(now=FIXED_NOW)
    event.close(now=FIXED_NOW)
    with pytest.raises(EventEditNotAllowedError):
        event.apply_edits(title="Поздно", now=FIXED_NOW)


def test_edit_noop_returns_false(future_window) -> None:
    event = _make_draft(future_window)
    assert event.apply_edits(title=event.title, now=FIXED_NOW) is False
