"""Общие фикстуры тестов домена events.

Глобальное тест-окружение (env-переменные) выставляет ``tests/conftest.py``;
здесь — доменные билдеры: фиксированное «сейчас», корректное окно, акторы
с разными ролями и предзаведённая категория.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.modules.events.application.dto import Actor
from app.modules.events.domain.entities import Category
from app.modules.events.domain.value_objects import EventWindow
from app.modules.identity.domain.entities import UserRole

# Фиксированный момент «сейчас» для детерминированных переходов.
FIXED_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return FIXED_NOW


@pytest.fixture
def future_window() -> EventWindow:
    """Корректное окно целиком в будущем относительно ``FIXED_NOW``."""
    return EventWindow(
        opens_at=FIXED_NOW + timedelta(days=1),
        closes_at=FIXED_NOW + timedelta(days=7),
        resolves_at=FIXED_NOW + timedelta(days=10),
    )


@pytest.fixture
def editor_actor() -> Actor:
    return Actor(user_id=uuid.uuid4(), role=UserRole.EDITOR)


@pytest.fixture
def user_actor() -> Actor:
    return Actor(user_id=uuid.uuid4(), role=UserRole.USER)


@pytest.fixture
def category() -> Category:
    """Предзаведённая категория для привязки событий."""
    return Category.create(slug="politics", title="Политика")
