"""FastAPI-роутер домена scoring.

Публичные чтения (лидерборды, калибровка профиля) идут из готовых агрегатов —
на чтении ничего не считается. Операционные триггеры (скоринг события, полный
пересчёт) — под RBAC; в проде их дёргает фоновый воркер, эндпоинты оставлены
для ручного запуска/отладки. Доменные ошибки маппятся в HTTP в ``app/main.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.modules.scoring.api.dependencies import (
    get_leaderboard_uc,
    get_recompute_ratings,
    get_score_event,
    get_user_calibration_uc,
    require_recompute_role,
    require_scoring_role,
)
from app.modules.scoring.api.schemas import (
    CalibrationResponse,
    LeaderboardResponse,
    RatingResponse,
    RecomputeRatingsResponse,
    ScoreEventResponse,
)
from app.modules.scoring.application.use_cases import (
    GetLeaderboard,
    GetUserCalibration,
    RecomputeRatings,
    ScoreEvent,
)
from app.modules.scoring.domain.entities import ScopeType

router = APIRouter(tags=["scoring"])


# ── Лидерборды (публичные чтения) ───────────────────────────────────────────


@router.get(
    "/leaderboards/global",
    response_model=LeaderboardResponse,
    summary="Глобальный топ предсказателей",
)
async def global_leaderboard(
    uc: Annotated[GetLeaderboard, Depends(get_leaderboard_uc)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeaderboardResponse:
    """Топ по усаженному превышению над толпой (больше ``skill_score`` = выше)."""
    ratings = await uc.execute(
        scope_type=ScopeType.GLOBAL, scope_id=None, limit=limit, offset=offset
    )
    return LeaderboardResponse(
        scope_type=ScopeType.GLOBAL,
        scope_id=None,
        entries=[RatingResponse.from_domain(r) for r in ratings],
    )


@router.get(
    "/leaderboards/categories/{category_id}",
    response_model=LeaderboardResponse,
    summary="Категорийный топ",
)
async def category_leaderboard(
    category_id: uuid.UUID,
    uc: Annotated[GetLeaderboard, Depends(get_leaderboard_uc)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeaderboardResponse:
    """Топ в категории.

    TODO(categories-integration): принимать ``slug`` и резолвить в id через
    домен categories (сейчас — прямой ``category_id``).
    """
    ratings = await uc.execute(
        scope_type=ScopeType.CATEGORY,
        scope_id=category_id,
        limit=limit,
        offset=offset,
    )
    return LeaderboardResponse(
        scope_type=ScopeType.CATEGORY,
        scope_id=category_id,
        entries=[RatingResponse.from_domain(r) for r in ratings],
    )


@router.get(
    "/leaderboards/seasons/{season_id}",
    response_model=LeaderboardResponse,
    summary="Сезонная лига",
)
async def season_leaderboard(
    season_id: uuid.UUID,
    uc: Annotated[GetLeaderboard, Depends(get_leaderboard_uc)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeaderboardResponse:
    """Топ в сезоне.

    TODO(seasons-integration): принимать ``slug`` и резолвить в id через домен
    seasons (сейчас — прямой ``season_id``).
    """
    ratings = await uc.execute(
        scope_type=ScopeType.SEASON, scope_id=season_id, limit=limit, offset=offset
    )
    return LeaderboardResponse(
        scope_type=ScopeType.SEASON,
        scope_id=season_id,
        entries=[RatingResponse.from_domain(r) for r in ratings],
    )


# ── Калибровка профиля (публичное чтение) ───────────────────────────────────


@router.get(
    "/users/{user_id}/calibration",
    response_model=CalibrationResponse,
    summary="Калибровка профиля (predicted vs actual)",
)
async def user_calibration(
    user_id: uuid.UUID,
    uc: Annotated[GetUserCalibration, Depends(get_user_calibration_uc)],
) -> CalibrationResponse:
    """Диаграмма надёжности по 5 градациям + декомпозиция Brier по Мёрфи.

    TODO(users-integration): резолвить публичный ``username`` в ``user_id``
    через профильный домен (контракт API задания — по ``username``).
    """
    report = await uc.execute(user_id=user_id)
    return CalibrationResponse.from_report(user_id, report)


# ── Операционные триггеры (RBAC; в проде — фоновый воркер) ───────────────────


@router.post(
    "/admin/events/{event_id}/score",
    response_model=ScoreEventResponse,
    summary="Запустить скоринг события (editor/arbiter/admin)",
)
async def score_event(
    event_id: uuid.UUID,
    uc: Annotated[ScoreEvent, Depends(get_score_event)],
    _role: Annotated[object, Depends(require_scoring_role)],
) -> ScoreEventResponse:
    """Считает Brier по всем прогнозам разрешённого события.

    TODO(scoring-infra): в проде вызывается ARQ-воркером ``score_event`` по
    доменному событию resolutions, а не вручную.
    """
    scored = await uc.execute(event_id=event_id)
    return ScoreEventResponse(event_id=event_id, scored=scored)


@router.post(
    "/admin/ratings/recompute",
    response_model=RecomputeRatingsResponse,
    summary="Полный пересчёт рейтингов (admin)",
)
async def recompute_ratings(
    uc: Annotated[RecomputeRatings, Depends(get_recompute_ratings)],
    _role: Annotated[object, Depends(require_recompute_role)],
    season_id: Annotated[uuid.UUID | None, Query()] = None,
) -> RecomputeRatingsResponse:
    """Перестраивает материализованные рейтинги по областям.

    TODO(scoring-infra): в проде — ночной full recompute + инкрементальный
    пересчёт затронутых ``(user, scope)`` после скоринга события.
    """
    upserted = await uc.execute(season_id=season_id)
    return RecomputeRatingsResponse(upserted=upserted)
