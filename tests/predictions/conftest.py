"""Общие фикстуры тестов домена predictions.

Глобальное тест-окружение (env-переменные) выставляет ``tests/conftest.py``;
здесь — доменные билдеры: фиксированное «сейчас», открытый/закрытый снимки
события и идентификаторы пользователя/события.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.predictions.domain.value_objects import EventSnapshot

# Фиксированный момент «сейчас» для детерминированной проверки дедлайна.
FIXED_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def user_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def event_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def open_snapshot(event_id: uuid.UUID) -> EventSnapshot:
    """Событие, принимающее прогнозы в момент ``FIXED_NOW``."""
    return EventSnapshot(
        event_id=event_id,
        is_open=True,
        opens_at=FIXED_NOW - timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=7),
    )


@pytest.fixture
def closed_snapshot(event_id: uuid.UUID) -> EventSnapshot:
    """Событие с истёкшим окном приёма (дедлайн в прошлом)."""
    return EventSnapshot(
        event_id=event_id,
        is_open=True,
        opens_at=FIXED_NOW - timedelta(days=7),
        closes_at=FIXED_NOW - timedelta(days=1),
    )
