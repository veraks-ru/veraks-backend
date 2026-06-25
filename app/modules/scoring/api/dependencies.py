"""Composition root модуля scoring (FastAPI DI).

Здесь — и только здесь — конкретные адаптеры связываются с портами и
собираются use-cases. В тестах достаточно переопределить провайдеры портов
(шлюз, писатель, репозиторий рейтингов, часы) и аутентификацию.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.identity.api.dependencies import CurrentUser
from app.modules.identity.domain.entities import UserRole
from app.modules.scoring.adapters.clock import SystemClock
from app.modules.scoring.adapters.rating_repository import SqlAlchemyRatingRepository
from app.modules.scoring.adapters.scoring_gateway import (
    SqlAlchemyEventScoringGateway,
    SqlAlchemyPredictionScoreWriter,
)
from app.modules.scoring.application.use_cases import (
    GetLeaderboard,
    GetUserCalibration,
    RecomputeRatings,
    ScoreEvent,
)
from app.modules.scoring.domain.policies import ensure_can_recompute, ensure_can_score
from app.modules.scoring.ports.clock import Clock
from app.modules.scoring.ports.gateways import (
    EventScoringGateway,
    PredictionScoreWriter,
)
from app.modules.scoring.ports.repositories import RatingRepository

SessionDep = Annotated[AsyncSession, Depends(get_session)]


# ── Порты → адаптеры ──────────────────────────────────────────────────────


def get_clock() -> Clock:
    """Серверные часы (переопределяются в тестах фиксированными)."""
    return SystemClock()


ClockDep = Annotated[Clock, Depends(get_clock)]


def get_event_scoring_gateway(
    session: SessionDep, clock: ClockDep
) -> EventScoringGateway:
    """Шлюз к данным разрешённых событий/прогнозов (поверх таблиц events/predictions).

    TODO(scoring-integration): прямое чтение соседних таблиц в монолите;
    заменить сетевым контрактом при выделении в сервис.
    """
    return SqlAlchemyEventScoringGateway(session, clock)


def get_prediction_score_writer(session: SessionDep) -> PredictionScoreWriter:
    """Писатель Brier-оценок обратно в ``predictions``."""
    return SqlAlchemyPredictionScoreWriter(session)


def get_rating_repository(session: SessionDep) -> RatingRepository:
    """Репозиторий материализованных рейтингов."""
    return SqlAlchemyRatingRepository(session)


GatewayDep = Annotated[EventScoringGateway, Depends(get_event_scoring_gateway)]
ScoreWriterDep = Annotated[PredictionScoreWriter, Depends(get_prediction_score_writer)]
RatingRepoDep = Annotated[RatingRepository, Depends(get_rating_repository)]


# ── RBAC ──────────────────────────────────────────────────────────────────


def require_scoring_role(current_user: CurrentUser) -> UserRole:
    """Гард: роль вправе запускать скоринг события (редактор/арбитр/админ)."""
    ensure_can_score(current_user.role)
    return current_user.role


def require_recompute_role(current_user: CurrentUser) -> UserRole:
    """Гард: роль вправе запускать полный пересчёт рейтингов (только админ)."""
    ensure_can_recompute(current_user.role)
    return current_user.role


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_score_event(
    gateway: GatewayDep, writer: ScoreWriterDep, clock: ClockDep
) -> ScoreEvent:
    """Use-case скоринга события (пер-прогнозный Brier)."""
    return ScoreEvent(gateway=gateway, writer=writer, clock=clock)


def get_recompute_ratings(
    gateway: GatewayDep, ratings: RatingRepoDep, clock: ClockDep
) -> RecomputeRatings:
    """Use-case полного пересчёта рейтингов."""
    return RecomputeRatings(gateway=gateway, ratings=ratings, clock=clock)


def get_leaderboard_uc(ratings: RatingRepoDep) -> GetLeaderboard:
    """Use-case чтения лидерборда области."""
    return GetLeaderboard(ratings=ratings)


def get_user_calibration_uc(gateway: GatewayDep) -> GetUserCalibration:
    """Use-case калибровки профиля."""
    return GetUserCalibration(gateway=gateway)
