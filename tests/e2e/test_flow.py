"""E2E полного цикла против реального Postgres (реальные адаптеры, без фейков I/O).

Happy-path: создание → публикация → приём прогнозов → закрытие → разрешение →
скоринг → пересчёт рейтингов → лидерборд. Плюс модерация предложенных событий.
Проверяет реальные FK/enum/UNIQUE/append-only и связку доменов end-to-end.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.repository import (
    SqlAlchemyCategoryRepository,
    SqlAlchemyEventRepository,
)
from app.modules.events.application.dto import Actor as EventActor, NewEventInput
from app.modules.events.application.use_cases import (
    ApproveEvent,
    ProposeEvent,
    RejectEvent,
)
from app.modules.events.domain.entities import EventStatus
from app.modules.identity.domain.entities import UserRole
from app.modules.resolutions.adapters.clock import SystemClock as ResolutionsClock
from app.modules.resolutions.adapters.event_gateway import (
    SqlAlchemyEventResolutionGateway,
)
from app.modules.resolutions.adapters.repositories import (
    SqlAlchemyResolutionRepository,
)
from app.modules.resolutions.application.dto import Actor as ResolutionActor
from app.modules.resolutions.application.use_cases import FixResolution
from app.modules.scoring.adapters.clock import SystemClock as ScoringClock
from app.modules.scoring.adapters.rating_repository import SqlAlchemyRatingRepository
from app.modules.scoring.adapters.scoring_gateway import (
    SqlAlchemyEventScoringGateway,
    SqlAlchemyPredictionScoreWriter,
)
from app.modules.scoring.adapters.season_config_gateway import (
    SqlAlchemySeasonConfigGateway,
)
from app.modules.scoring.application.use_cases import RecomputeRatings, ScoreEvent
from app.modules.scoring.domain.entities import ScopeType
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from tests.e2e.helpers import (
    BASE,
    CLOSES_AT,
    OPENS_AT,
    RESOLVES_AT,
    FixedClock,
    add_active_season,
    add_category,
    add_open_event,
    add_user,
    place_locked_predictions,
)

pytestmark = pytest.mark.asyncio


class _FakeGate:
    def __init__(self, active: bool) -> None:
        self._active = active

    async def has_active_subscription(self, user_id, now) -> bool:  # noqa: ANN001
        return self._active


class _FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def emit(self, **kwargs) -> None:  # noqa: ANN003
        self.calls.append(kwargs)


async def test_happy_path_create_to_ratings(session: AsyncSession) -> None:
    admin = await add_user(session, username="arbiter1", role=UserRole.ADMIN)
    voters = [await add_user(session, username=f"voter{i}") for i in range(5)]
    category = await add_category(session)
    season = await add_active_season(session)
    await session.flush()

    event = await add_open_event(
        session,
        category_id=category.id,
        created_by=admin.id,
        season_id=season.id,
    )
    assert event.status is EventStatus.OPEN

    locked = await place_locked_predictions(
        session, event_id=event.id, user_ids=[v.id for v in voters]
    )
    assert locked == 5

    # Закрытие приёма (OPEN → CLOSED) — предусловие разрешения.
    repo = SqlAlchemyEventRepository(session)
    event.close(now=CLOSES_AT)
    await repo.update(event)

    # Разрешение с нулевым окном оспаривания → сразу скорится.
    await FixResolution(
        resolutions=SqlAlchemyResolutionRepository(session),
        events=SqlAlchemyEventResolutionGateway(session),
        audit=SqlAlchemyAuditTrail(session),
        clock=FixedClock(RESOLVES_AT),
        dispute_window=timedelta(0),
    ).execute(
        event_id=event.id,
        actor=ResolutionActor(user_id=admin.id, role=UserRole.ADMIN),
        outcome=True,
        source_reference="Пресс-релиз ЦБ",
    )

    clock = ScoringClock()
    gateway = SqlAlchemyEventScoringGateway(session, clock)
    scored = await ScoreEvent(
        gateway=gateway,
        writer=SqlAlchemyPredictionScoreWriter(session),
        clock=clock,
    ).execute(event_id=event.id)
    assert scored == 5

    upserted = await RecomputeRatings(
        gateway=gateway,
        ratings=SqlAlchemyRatingRepository(session),
        clock=clock,
        season_config=SqlAlchemySeasonConfigGateway(session),
    ).execute()
    assert upserted > 0

    board = await SqlAlchemyRatingRepository(session).leaderboard(
        ScopeType.GLOBAL, None, limit=50
    )
    assert len(board) == 5
    assert {r.rank for r in board} == {1, 2, 3, 4, 5}
    # Все Brier проставлены в predictions.
    scored_rows = (
        await session.execute(
            text("SELECT count(*) FROM predictions WHERE brier_score IS NOT NULL")
        )
    ).scalar_one()
    assert scored_rows == 5
    await session.commit()


async def test_propose_reject_then_approve(session: AsyncSession) -> None:
    author = await add_user(session, username="author1")
    moderator = await add_user(session, username="mod1", role=UserRole.ADMIN)
    category = await add_category(session)
    await session.flush()

    def _input() -> NewEventInput:
        return NewEventInput(
            title="Предложенное событие",
            description="e2e propose",
            category_id=category.id,
            opens_at=OPENS_AT,
            closes_at=CLOSES_AT,
            resolves_at=RESOLVES_AT,
            resolution_source="src",
            resolution_criteria="crit",
        )

    propose = ProposeEvent(
        events=SqlAlchemyEventRepository(session),
        categories=SqlAlchemyCategoryRepository(session),
        clock=FixedClock(BASE),
        audit=SqlAlchemyAuditTrail(session),
        subscriptions=_FakeGate(active=True),
    )
    notifier = _FakeNotifier()

    proposed = await propose.execute(
        actor=EventActor(user_id=author.id, role=UserRole.USER), data=_input()
    )
    assert proposed.status is EventStatus.PROPOSED

    rejected = await RejectEvent(
        events=SqlAlchemyEventRepository(session),
        clock=FixedClock(BASE),
        audit=SqlAlchemyAuditTrail(session),
        notifier=notifier,
    ).execute(
        actor=EventActor(user_id=moderator.id, role=UserRole.ADMIN),
        event_id=proposed.id,
        reason="Источник неоднозначен",
    )
    assert rejected.status is EventStatus.CANCELLED
    assert notifier.calls  # автору ушло уведомление об отклонении

    # Второе предложение — одобряем (PROPOSED → DRAFT).
    proposed2 = await propose.execute(
        actor=EventActor(user_id=author.id, role=UserRole.USER), data=_input()
    )
    approved = await ApproveEvent(
        events=SqlAlchemyEventRepository(session),
        clock=FixedClock(BASE),
        audit=SqlAlchemyAuditTrail(session),
        notifier=notifier,
    ).execute(
        actor=EventActor(user_id=moderator.id, role=UserRole.ADMIN),
        event_id=proposed2.id,
    )
    assert approved.status is EventStatus.DRAFT
    await session.commit()
