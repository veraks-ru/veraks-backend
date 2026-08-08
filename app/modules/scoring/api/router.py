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

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.identity.domain.entities import User
from app.modules.scoring.api.dependencies import (
    get_finalize_season,
    get_leaderboard_uc,
    get_profile_summary_uc,
    get_recalibrate_gradations,
    get_recompute_ratings,
    get_score_event,
    get_season_leaderboard_uc,
    get_season_qualification_uc,
    get_season_standing_uc,
    get_user_calibration_uc,
    require_recompute_role,
    require_scoring_role,
    require_season_transition_role,
)
from app.modules.scoring.api.schemas import (
    CalibrationResponse,
    FinalizeSeasonResponse,
    GradationRecalibrationResponse,
    LeaderboardResponse,
    ProfileSummaryResponse,
    QualificationResponse,
    RatingResponse,
    RecomputeRatingsResponse,
    ScoreEventResponse,
    SeasonStandingResponse,
)
from app.modules.scoring.application.seasons_coordination import FinalizeSeason
from app.modules.scoring.application.use_cases import (
    GetLeaderboard,
    GetProfileSummary,
    GetSeasonLeaderboard,
    GetSeasonQualification,
    GetSeasonStanding,
    GetUserCalibration,
    RecalibrateSeasonGradations,
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
    qualified_only: Annotated[bool, Query()] = True,
) -> LeaderboardResponse:
    """Топ по усаженному превышению над толпой (больше ``skill_score`` = выше).

    ``qualified_only`` (по умолчанию ``true``) — порог участия (PRD §4.6):
    скрывает предсказателей с числом разрешённых прогнозов ниже
    ``LEADERBOARD_MIN_RESOLVED_GLOBAL``; ``false`` отдаёт всех — для
    админки/отладки.
    """
    ratings, min_resolved = await uc.execute(
        scope_type=ScopeType.GLOBAL,
        scope_id=None,
        limit=limit,
        offset=offset,
        qualified_only=qualified_only,
    )
    return LeaderboardResponse(
        scope_type=ScopeType.GLOBAL,
        scope_id=None,
        entries=[RatingResponse.from_domain(r) for r in ratings],
        min_resolved=min_resolved,
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
    qualified_only: Annotated[bool, Query()] = True,
) -> LeaderboardResponse:
    """Топ в категории.

    ``qualified_only`` (по умолчанию ``true``) — порог участия (PRD §4.6):
    скрывает предсказателей с числом разрешённых прогнозов в категории ниже
    ``LEADERBOARD_MIN_RESOLVED_CATEGORY``; ``false`` отдаёт всех — для
    админки/отладки.

    TODO(categories-integration): принимать ``slug`` и резолвить в id через
    домен categories (сейчас — прямой ``category_id``).
    """
    ratings, min_resolved = await uc.execute(
        scope_type=ScopeType.CATEGORY,
        scope_id=category_id,
        limit=limit,
        offset=offset,
        qualified_only=qualified_only,
    )
    return LeaderboardResponse(
        scope_type=ScopeType.CATEGORY,
        scope_id=category_id,
        entries=[RatingResponse.from_domain(r) for r in ratings],
        min_resolved=min_resolved,
    )


@router.get(
    "/leaderboards/seasons/{slug}",
    response_model=LeaderboardResponse,
    summary="Сезонная лига",
)
async def season_leaderboard(
    slug: str,
    uc: Annotated[GetSeasonLeaderboard, Depends(get_season_leaderboard_uc)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    qualified_only: Annotated[bool, Query()] = False,
) -> LeaderboardResponse:
    """Топ в сезоне по slug — в порядке призовых мест.

    Сезонный ``rank`` уже призовой: квалифицированные к призам идут выше
    остальных независимо от ``skill_score``. По умолчанию отдаём всех
    (неквалифицированные — ниже призовой зоны, с ``qualified=false``);
    ``qualified_only=true`` оставляет только призовую зону.
    """
    season_id, ratings = await uc.execute(
        slug=slug, limit=limit, offset=offset, qualified_only=qualified_only
    )
    return LeaderboardResponse(
        scope_type=ScopeType.SEASON,
        scope_id=season_id,
        entries=[RatingResponse.from_domain(r) for r in ratings],
    )


@router.get(
    "/leaderboards/seasons/{slug}/me",
    response_model=SeasonStandingResponse,
    summary="Своя позиция в сезоне + разбор квалификации",
)
async def my_season_standing(
    slug: str,
    current_user: CurrentUser,
    uc: Annotated[GetSeasonStanding, Depends(get_season_standing_uc)],
) -> SeasonStandingResponse:
    """Закреплённая строка «вы» под сезонной таблицей.

    Лидерборд страничный, поэтому своя позиция может быть за пределами выдачи —
    здесь она отдаётся всегда, вместе с разбором порогов (объём / разнообразие
    категорий / охват сложности), чтобы было видно не только место, но и что
    именно отделяет от призового зачёта.
    """
    standing = await uc.execute(user_id=current_user.id, slug=slug)
    return SeasonStandingResponse.from_domain(standing)


# ── Калибровка профиля (публичное чтение) ───────────────────────────────────


@router.get(
    "/users/{username}/calibration",
    response_model=CalibrationResponse,
    summary="Калибровка профиля (predicted vs actual)",
)
async def user_calibration(
    username: str,
    uc: Annotated[GetUserCalibration, Depends(get_user_calibration_uc)],
) -> CalibrationResponse:
    """Диаграмма надёжности по 5 градациям + декомпозиция Brier по Мёрфи.

    Публичный профиль по ``username`` (контракт API задания); хэндл резолвится
    в ``user_id`` через профильный шлюз. Неизвестный профиль → 404.
    """
    user_id, report = await uc.execute(username=username)
    return CalibrationResponse.from_report(user_id, report)


@router.get(
    "/users/{username}/summary",
    response_model=ProfileSummaryResponse,
    summary="Сводка профиля: global / по категориям / активный сезон",
)
async def user_summary(
    username: str,
    uc: Annotated[GetProfileSummary, Depends(get_profile_summary_uc)],
) -> ProfileSummaryResponse:
    """Готовые срезы рейтинга пользователя одним ответом (без пересчёта на чтении).

    Публичный профиль по ``username`` (как у калибровки); неизвестный/не-ACTIVE
    профиль → 404. Пользователь без рейтингов получает пустую сводку
    (``global``/``season`` — ``null``, ``categories`` — ``[]``), а не 500.
    """
    summary = await uc.execute(username=username)
    return ProfileSummaryResponse.from_domain(summary)


@router.get(
    "/users/{user_id}/seasons/{slug}/qualification",
    response_model=QualificationResponse,
    summary="Квалификация пользователя в сезоне (почему да/нет)",
)
async def user_season_qualification(
    user_id: uuid.UUID,
    slug: str,
    uc: Annotated[GetSeasonQualification, Depends(get_season_qualification_uc)],
) -> QualificationResponse:
    """Разбор порогов квалификации к призам сезона (объём/разнообразие/охват)."""
    result = await uc.execute(user_id=user_id, slug=slug)
    return QualificationResponse.from_domain(result)


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


@router.get(
    "/admin/seasons/{season_id}/recalibration",
    response_model=list[GradationRecalibrationResponse],
    summary="Предложение межсезонной рекалибровки градаций (admin)",
)
async def season_recalibration(
    season_id: uuid.UUID,
    uc: Annotated[
        RecalibrateSeasonGradations, Depends(get_recalibrate_gradations)
    ],
    _role: Annotated[object, Depends(require_recompute_role)],
) -> list[GradationRecalibrationResponse]:
    """Пересчёт номиналов градаций по фактическим частотам прошедшего сезона.

    Возвращает предложение нового маппинга (монотонного) для следующего сезона;
    заморозка в ``LeagueConfig`` — отдельный шаг активации (условия конкурса не
    меняются по ходу сезона).
    """
    items = await uc.execute(season_id=season_id)
    return [GradationRecalibrationResponse.from_domain(i) for i in items]


@router.post(
    "/admin/seasons/{season_id}/finalize",
    response_model=FinalizeSeasonResponse,
    summary="Финализировать сезон (admin, maker-checker роль)",
)
async def finalize_season(
    season_id: uuid.UUID,
    uc: Annotated[FinalizeSeason, Depends(get_finalize_season)],
    actor: Annotated[User, Depends(require_season_transition_role)],
) -> FinalizeSeasonResponse:
    """Ручной admin-триггер ``active → finished`` с пересчётом и снапшотом призёров.

    Идемпотентно (повтор по завершённому сезону — no-op) и блокируется при
    открытых спорах. Рядом с автоматическим ``season_roll`` в воркере — ручной
    override (дизайн §6.5).

    TODO(scoring-infra): в проде также дёргается воркером ``season_roll`` по
    истечении ``ends_at`` (когда включён ``seasons_auto_finalize``).
    """
    result = await uc.execute(season_id=season_id, actor_id=actor.id)
    return FinalizeSeasonResponse(
        season_id=season_id,
        finalized=result.finalized,
        qualified_count=result.qualified_count,
        total_participants=result.total_participants,
    )
