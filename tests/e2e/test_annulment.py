"""E2E аннулирования события против реального Postgres.

Главный инвариант T5 (PRD §7.5/§4.8, ст. 1058 ГК РФ): аннулированное событие
исключается из ВСЕХ рейтингов и калибровки. Проверить это фейками нельзя —
выборки скоринга фильтруют по нативному enum ``event_status`` прямо в SQL,
поэтому тест гоняет настоящие адаптеры и настоящую БД:

  * два события скорятся и дают ``n_resolved = 2`` в рейтинге;
  * после аннулирования второго пересчёт видит только первое;
  * повторный скоринг аннулированного события невозможен (защита от
    «дозаписи» по идемпотентному диспатчу ``resolution_scoring_dispatches``).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.repository import SqlAlchemyEventRepository
from app.modules.events.application.dto import Actor as EventActor
from app.modules.events.application.use_cases import AnnulEvent
from app.modules.events.domain.entities import Event, EventStatus
from app.modules.identity.domain.entities import UserRole
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
from app.modules.scoring.domain.errors import EventNotResolvedError
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from tests.e2e.helpers import (
    CLOSES_AT,
    RESOLVES_AT,
    FixedClock,
    add_active_season,
    add_category,
    add_open_event,
    add_user,
    place_locked_predictions,
)

pytestmark = pytest.mark.asyncio


async def _resolve_and_score(
    session: AsyncSession, *, event: Event, admin_id, clock: ScoringClock
) -> None:
    """Закрывает приём, фиксирует исход с нулевым окном и скорит событие."""
    repo = SqlAlchemyEventRepository(session)
    event.close(now=CLOSES_AT)
    await repo.update(event)
    await FixResolution(
        resolutions=SqlAlchemyResolutionRepository(session),
        events=SqlAlchemyEventResolutionGateway(session),
        audit=SqlAlchemyAuditTrail(session),
        clock=FixedClock(RESOLVES_AT),
        dispute_window=timedelta(0),
    ).execute(
        event_id=event.id,
        actor=ResolutionActor(user_id=admin_id, role=UserRole.ADMIN),
        outcome=True,
        source_reference="Пресс-релиз",
    )
    await ScoreEvent(
        gateway=SqlAlchemyEventScoringGateway(session, clock),
        writer=SqlAlchemyPredictionScoreWriter(session),
        clock=clock,
    ).execute(event_id=event.id)


async def test_annulled_event_drops_out_of_ratings_and_calibration(
    session: AsyncSession,
) -> None:
    admin = await add_user(session, username="arbiter1", role=UserRole.ARBITER)
    voters = [await add_user(session, username=f"voter{i}") for i in range(5)]
    category = await add_category(session)
    season = await add_active_season(session)
    await session.flush()

    clock = ScoringClock()
    events = []
    for _ in range(2):
        event = await add_open_event(
            session,
            category_id=category.id,
            created_by=admin.id,
            season_id=season.id,
        )
        await place_locked_predictions(
            session, event_id=event.id, user_ids=[v.id for v in voters]
        )
        await _resolve_and_score(session, event=event, admin_id=admin.id, clock=clock)
        events.append(event)
    good, bad = events

    gateway = SqlAlchemyEventScoringGateway(session, clock)
    ratings = SqlAlchemyRatingRepository(session)

    def _recompute() -> RecomputeRatings:
        # Свежий шлюз на каждый пересчёт: он кэширует сетки градаций сезона.
        return RecomputeRatings(
            gateway=SqlAlchemyEventScoringGateway(session, clock),
            ratings=ratings,
            clock=clock,
            season_config=SqlAlchemySeasonConfigGateway(session),
        )

    await _recompute().execute()
    board = await ratings.leaderboard(ScopeType.GLOBAL, None, limit=50)
    assert len(board) == 5
    assert {r.n_resolved for r in board} == {2}
    assert len(await gateway.list_resolved_events()) == 2
    assert len(await gateway.list_user_calibration_entries(voters[0].id)) == 2

    # ── Аннулирование второго события (арбитр, обязательная причина) ────────
    annulled = await AnnulEvent(
        events=SqlAlchemyEventRepository(session),
        clock=FixedClock(RESOLVES_AT + timedelta(days=1)),
        audit=SqlAlchemyAuditTrail(session),
    ).execute(
        actor=EventActor(user_id=admin.id, role=UserRole.ARBITER),
        event_id=bad.id,
        reason="Формулировка допускала два толкования",
    )
    assert annulled.status is EventStatus.ANNULLED

    # Пересчёт затронутых срезов — как это делает роутер после аннулирования.
    await _recompute().execute(
        scopes=RecomputeRatings.touched_scopes(
            category_id=bad.category_id, season_id=bad.season_id
        )
    )

    fresh = SqlAlchemyEventScoringGateway(session, clock)
    resolved_ids = [e.event_id for e in await fresh.list_resolved_events()]
    assert resolved_ids == [good.id]

    for scope_type, scope_id in (
        (ScopeType.GLOBAL, None),
        (ScopeType.CATEGORY, category.id),
        (ScopeType.SEASON, season.id),
    ):
        board = await ratings.leaderboard(scope_type, scope_id, limit=50)
        assert board, f"срез {scope_type} опустел"
        assert {r.n_resolved for r in board} == {1}, f"срез {scope_type}"

    # Калибровка профиля тоже не видит аннулированное событие, хотя прогнозы
    # по нему остались со старым brier_score/scored_at.
    assert len(await fresh.list_user_calibration_entries(voters[0].id)) == 1
    assert len(await fresh.list_season_calibration_entries(season.id)) == 5

    # Повторный диспатч скоринга по аннулированному событию — отказ.
    with pytest.raises(EventNotResolvedError):
        await ScoreEvent(
            gateway=SqlAlchemyEventScoringGateway(session, clock),
            writer=SqlAlchemyPredictionScoreWriter(session),
            clock=clock,
        ).execute(event_id=bad.id)

    await session.commit()
