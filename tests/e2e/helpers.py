"""Билдеры для e2e-сценариев: реальные сущности через реальные репозитории.

Всё пишется в настоящий Postgres той же сессией теста (репозитории делают
``flush``; тест коммитит). Порядок FK: пользователи/категория/сезон → событие →
прогнозы.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from app.modules.events.adapters.repository import (
    SqlAlchemyCategoryRepository,
    SqlAlchemyEventRepository,
)
from app.modules.events.domain.entities import Category, Event
from app.modules.events.domain.value_objects import EventWindow
from app.modules.identity.adapters.repository import SqlAlchemyUserRepository
from app.modules.identity.domain.entities import User, UserRole
from app.modules.predictions.adapters.repository import (
    SqlAlchemyPredictionRepository,
)
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig

UTC = timezone.utc
# Времена окна события — в прошлом относительно реальных «сейчас» (для скоринга).
BASE = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
OPENS_AT = BASE
CLOSES_AT = BASE + timedelta(days=7)
RESOLVES_AT = BASE + timedelta(days=9)


class FixedClock:
    """Часы с фиксированным «сейчас» для детерминированных переходов."""

    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def add_user(
    session, *, username: str, role: UserRole = UserRole.USER
) -> User:
    user = User(
        esia_oid=f"oid-{username}",
        snils_hash=f"hash-{username}",
        username=username,
        display_name=username.title(),
        real_name_enc=None,
        role=role,
    )
    saved = await SqlAlchemyUserRepository(session).add(user)
    return saved


async def add_category(session, *, slug: str = "finance") -> Category:
    category = Category.create(slug=slug, title="Финансы")
    return await SqlAlchemyCategoryRepository(session).add(category)


async def add_active_season(session, *, slug: str = "s1") -> Season:
    season = Season(
        slug=slug,
        title="Сезон I",
        starts_at=BASE - timedelta(days=1),
        ends_at=RESOLVES_AT + timedelta(days=30),
        status=SeasonStatus.ACTIVE,
        league_config=LeagueConfig.default(),
    )
    await SqlAlchemySeasonRepository(session).add(season)
    return season


async def add_open_event(
    session,
    *,
    category_id: uuid.UUID,
    created_by: uuid.UUID,
    season_id: uuid.UUID | None,
) -> Event:
    """Черновик → публикация: событие в статусе OPEN, готовое к приёму."""
    event = Event.create_draft(
        title="Ключевая ставка ЦБ будет снижена",
        description="Демо-событие e2e.",
        category_id=category_id,
        created_by=created_by,
        window=EventWindow(
            opens_at=OPENS_AT, closes_at=CLOSES_AT, resolves_at=RESOLVES_AT
        ),
        resolution_source="Официальный источник",
        resolution_criteria="Засчитывается ДА при подтверждении.",
        season_id=season_id,
        now=OPENS_AT,
    )
    repo = SqlAlchemyEventRepository(session)
    event = await repo.add(event)
    event.publish(now=OPENS_AT)
    return await repo.update(event)


async def place_locked_predictions(
    session, *, event_id: uuid.UUID, user_ids: list[uuid.UUID]
) -> int:
    """Кладёт по прогнозу от каждого пользователя и блокирует их (для скоринга)."""
    grades = [
        ConfidenceGrade.DEFINITELY_NO,
        ConfidenceGrade.PROBABLY_NO,
        ConfidenceGrade.FIFTY_FIFTY,
        ConfidenceGrade.PROBABLY_YES,
        ConfidenceGrade.DEFINITELY_YES,
    ]
    repo = SqlAlchemyPredictionRepository(session)
    for i, uid in enumerate(user_ids):
        await repo.add(
            Prediction.place(
                user_id=uid,
                event_id=event_id,
                grade=grades[i % len(grades)],
                now=OPENS_AT + timedelta(days=1),
            )
        )
    return await repo.lock_for_event(event_id, now=CLOSES_AT)
