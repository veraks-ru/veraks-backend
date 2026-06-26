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
from app.modules.scoring.adapters.season_config_gateway import (
    SqlAlchemySeasonConfigGateway,
)
from app.modules.scoring.adapters.user_gateway import SqlAlchemyUserDirectory
from app.modules.scoring.application.use_cases import (
    GetLeaderboard,
    GetSeasonLeaderboard,
    GetSeasonQualification,
    GetUserCalibration,
    RecomputeRatings,
    ScoreEvent,
)
from app.modules.scoring.application.seasons_coordination import FinalizeSeason
from app.modules.scoring.domain.policies import ensure_can_recompute, ensure_can_score
from app.modules.scoring.ports.clock import Clock
from app.modules.scoring.ports.gateways import (
    EventScoringGateway,
    PredictionScoreWriter,
)
from app.modules.scoring.ports.repositories import RatingRepository
from app.modules.scoring.ports.season_config import SeasonConfigGateway
from app.modules.scoring.ports.users import UserDirectory
from app.modules.resolutions.adapters.dispute_guard import ResolutionDisputeGuard
from app.modules.resolutions.adapters.repositories import SqlAlchemyDisputeRepository
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.modules.seasons.domain.policies import ensure_can_transition
from app.modules.seasons.ports.gateways import DisputeGuard
from app.modules.seasons.ports.repositories import SeasonRepository

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


def get_season_config_gateway(session: SessionDep) -> SeasonConfigGateway:
    """Шлюз к конфигурации сезонов (читает таблицу ``seasons`` напрямую).

    TODO(scoring-integration): прямое чтение таблицы соседнего домена в
    монолите; заменить сетевым контрактом при выделении seasons в сервис.
    """
    return SqlAlchemySeasonConfigGateway(session)


def get_season_repository(session: SessionDep) -> SeasonRepository:
    """Репозиторий сезонов (для финализации — нужна блокировка ``FOR UPDATE``)."""
    return SqlAlchemySeasonRepository(session)


def get_dispute_guard(session: SessionDep) -> DisputeGuard:
    """Проверка открытых споров по событиям сезона (домен resolutions).

    Делит сессию запроса с финализацией — проверка идёт в той же транзакции.
    """
    return ResolutionDisputeGuard(SqlAlchemyDisputeRepository(session))


GatewayDep = Annotated[EventScoringGateway, Depends(get_event_scoring_gateway)]
ScoreWriterDep = Annotated[PredictionScoreWriter, Depends(get_prediction_score_writer)]
RatingRepoDep = Annotated[RatingRepository, Depends(get_rating_repository)]
SeasonConfigDep = Annotated[
    SeasonConfigGateway, Depends(get_season_config_gateway)
]
SeasonRepoDep = Annotated[SeasonRepository, Depends(get_season_repository)]
DisputeGuardDep = Annotated[DisputeGuard, Depends(get_dispute_guard)]


# ── RBAC ──────────────────────────────────────────────────────────────────


def require_scoring_role(current_user: CurrentUser) -> UserRole:
    """Гард: роль вправе запускать скоринг события (редактор/арбитр/админ)."""
    ensure_can_score(current_user.role)
    return current_user.role


def require_recompute_role(current_user: CurrentUser) -> UserRole:
    """Гард: роль вправе запускать полный пересчёт рейтингов (только админ)."""
    ensure_can_recompute(current_user.role)
    return current_user.role


def require_season_transition_role(current_user: CurrentUser) -> UserRole:
    """Гард: роль вправе финализировать сезон (только админ; разделение ролей)."""
    ensure_can_transition(current_user.role)
    return current_user.role


# ── Use-cases ─────────────────────────────────────────────────────────────


def get_score_event(
    gateway: GatewayDep, writer: ScoreWriterDep, clock: ClockDep
) -> ScoreEvent:
    """Use-case скоринга события (пер-прогнозный Brier)."""
    return ScoreEvent(gateway=gateway, writer=writer, clock=clock)


def get_recompute_ratings(
    gateway: GatewayDep,
    ratings: RatingRepoDep,
    clock: ClockDep,
    season_config: SeasonConfigDep,
) -> RecomputeRatings:
    """Use-case полного пересчёта рейтингов."""
    return RecomputeRatings(
        gateway=gateway, ratings=ratings, clock=clock, season_config=season_config
    )


def get_leaderboard_uc(ratings: RatingRepoDep) -> GetLeaderboard:
    """Use-case чтения лидерборда области."""
    return GetLeaderboard(ratings=ratings)


def get_season_leaderboard_uc(
    ratings: RatingRepoDep, season_config: SeasonConfigDep
) -> GetSeasonLeaderboard:
    """Use-case сезонного лидерборда по slug (с фильтром квалификации)."""
    return GetSeasonLeaderboard(ratings=ratings, season_config=season_config)


def get_season_qualification_uc(
    gateway: GatewayDep, season_config: SeasonConfigDep
) -> GetSeasonQualification:
    """Use-case разбора квалификации пользователя в сезоне."""
    return GetSeasonQualification(gateway=gateway, season_config=season_config)


def get_user_directory(session: SessionDep) -> UserDirectory:
    """Резолв пользователя по хэндлу (чтение таблицы users в монолите)."""
    return SqlAlchemyUserDirectory(session)


UserDirectoryDep = Annotated[UserDirectory, Depends(get_user_directory)]


def get_user_calibration_uc(
    gateway: GatewayDep, users: UserDirectoryDep
) -> GetUserCalibration:
    """Use-case калибровки профиля по хэндлу."""
    return GetUserCalibration(gateway=gateway, users=users)


def get_finalize_season(
    season_repo: SeasonRepoDep,
    dispute_guard: DisputeGuardDep,
    gateway: GatewayDep,
    ratings: RatingRepoDep,
    clock: ClockDep,
    season_config: SeasonConfigDep,
) -> FinalizeSeason:
    """Координатор финализации сезона (пересчёт + неизменяемый снапшот призёров).

    Делит сессию (а значит, транзакцию) со всеми портами запроса — пересчёт,
    запись финализации и перевод статуса коммитятся разом (атомарность, §6.2).
    """
    recompute = RecomputeRatings(
        gateway=gateway, ratings=ratings, clock=clock, season_config=season_config
    )
    return FinalizeSeason(
        seasons=season_repo,
        dispute_guard=dispute_guard,
        recompute=recompute,
        ratings=ratings,
        clock=clock,
    )
