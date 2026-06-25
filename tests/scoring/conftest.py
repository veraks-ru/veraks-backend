"""Общие фикстуры тестов домена scoring.

Глобальное тест-окружение (env) задаёт ``tests/conftest.py``; здесь —
фиксированное «сейчас» и билдеры разрешённых событий для use-case тестов.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.modules.scoring.domain.value_objects import PredictionVote, ResolvedEvent

FIXED_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def now() -> datetime:
    return FIXED_NOW


def make_event(
    *,
    outcome: int,
    probabilities: list[float],
    category_id: uuid.UUID | None = None,
    season_id: uuid.UUID | None = None,
    user_ids: list[uuid.UUID] | None = None,
) -> tuple[ResolvedEvent, list[uuid.UUID]]:
    """Строит разрешённое событие с голосами; возвращает (событие, user_ids)."""
    ids = user_ids or [uuid.uuid4() for _ in probabilities]
    votes = tuple(
        PredictionVote(user_id=uid, probability=p)
        for uid, p in zip(ids, probabilities, strict=True)
    )
    event = ResolvedEvent(
        event_id=uuid.uuid4(),
        category_id=category_id or uuid.uuid4(),
        season_id=season_id,
        outcome=outcome,
        votes=votes,
    )
    return event, ids
