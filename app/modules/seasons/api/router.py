"""FastAPI-роутер домена seasons.

Публичные чтения (``/seasons``) и admin-операции жизненного цикла. Активация —
ручной admin-триггер (рядом с автоматическим таймерным ``season_roll`` в
воркере); финализация живёт в scoring (нужен финальный пересчёт рейтингов).
RBAC проверяется в use-cases по роли текущего пользователя.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.seasons.api.dependencies import (
    get_activate_season,
    get_create_season,
    get_get_season,
    get_list_seasons,
    get_update_season,
)
from app.modules.seasons.api.schemas import (
    ActivateSeasonRequest,
    CreateSeasonRequest,
    SeasonListResponse,
    SeasonResponse,
    UpdateSeasonRequest,
)
from app.modules.seasons.application.use_cases import (
    ActivateSeason,
    CreateSeason,
    GetSeason,
    ListSeasons,
    UpdateSeason,
)
from app.modules.seasons.domain.entities import SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig

router = APIRouter(tags=["seasons"])


# ── Публичные чтения ─────────────────────────────────────────────────────────


@router.get("/seasons", response_model=SeasonListResponse, summary="Список сезонов")
async def list_seasons(
    uc: Annotated[ListSeasons, Depends(get_list_seasons)],
    season_status: Annotated[SeasonStatus | None, Query(alias="status")] = None,
) -> SeasonListResponse:
    """Сезоны, опционально отфильтрованные по статусу."""
    seasons = await uc.execute(status=season_status)
    return SeasonListResponse(items=[SeasonResponse.from_domain(s) for s in seasons])


@router.get(
    "/seasons/{slug}", response_model=SeasonResponse, summary="Сезон по slug"
)
async def get_season(
    slug: str,
    uc: Annotated[GetSeason, Depends(get_get_season)],
) -> SeasonResponse:
    """Детали сезона (включая снапшот правил лиги, если активирован)."""
    return SeasonResponse.from_domain(await uc.execute(slug=slug))


# ── Admin-операции ────────────────────────────────────────────────────────────


@router.post(
    "/admin/seasons",
    response_model=SeasonResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Завести сезон (editor/admin)",
)
async def create_season(
    body: CreateSeasonRequest,
    current_user: CurrentUser,
    uc: Annotated[CreateSeason, Depends(get_create_season)],
) -> SeasonResponse:
    season = await uc.execute(
        slug=body.slug,
        title=body.title,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
        actor_role=current_user.role,
    )
    return SeasonResponse.from_domain(season)


@router.patch(
    "/admin/seasons/{season_id}",
    response_model=SeasonResponse,
    summary="Править сезон до активации (editor/admin)",
)
async def update_season(
    season_id: uuid.UUID,
    body: UpdateSeasonRequest,
    current_user: CurrentUser,
    uc: Annotated[UpdateSeason, Depends(get_update_season)],
) -> SeasonResponse:
    season = await uc.execute(
        season_id=season_id,
        actor_role=current_user.role,
        title=body.title,
        slug=body.slug,
        starts_at=body.starts_at,
        ends_at=body.ends_at,
    )
    return SeasonResponse.from_domain(season)


@router.post(
    "/admin/seasons/{season_id}/activate",
    response_model=SeasonResponse,
    summary="Активировать сезон, заморозив правила лиги (admin)",
)
async def activate_season(
    season_id: uuid.UUID,
    body: ActivateSeasonRequest,
    current_user: CurrentUser,
    uc: Annotated[ActivateSeason, Depends(get_activate_season)],
) -> SeasonResponse:
    """Ручной admin-триггер ``upcoming → active``.

    Снапшот правил: кастомный из тела или нейтральный дефолт seasons.
    """
    config = (
        body.league_config.to_domain()
        if body.league_config is not None
        else LeagueConfig.default()
    )
    season = await uc.execute(
        season_id=season_id, config=config, actor_role=current_user.role
    )
    return SeasonResponse.from_domain(season)
