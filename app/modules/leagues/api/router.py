"""Роутер лиг и дивизионов."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.leagues.api.dependencies import (
    get_apply_promotion,
    get_create_league,
    get_delete_league,
    get_division_standings,
    get_join_league,
    get_league_standings,
    get_leave_league,
    get_list_all_leagues,
    get_list_my_leagues,
    get_rename_league,
    get_seed_divisions,
    require_admin,
)
from app.modules.leagues.api.schemas import (
    ApplyPromotionRequest,
    DivisionStandingsResponse,
    LeagueCreateRequest,
    LeagueJoinRequest,
    LeagueListResponse,
    LeagueRenameRequest,
    LeagueResponse,
    LeagueStandingsResponse,
    SeedDivisionsRequest,
)
from app.modules.leagues.application.use_cases import (
    ApplyPromotionRelegation,
    CreateLeague,
    DeleteLeague,
    GetDivisionStandings,
    GetLeagueStandings,
    JoinLeague,
    LeaveLeague,
    ListAllLeagues,
    ListMyLeagues,
    RenameLeague,
    SeedSeasonDivisions,
)

router = APIRouter(tags=["leagues"])


# ── Приватные лиги ───────────────────────────────────────────────────────────


@router.post(
    "/leagues",
    response_model=LeagueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать приватную лигу",
)
async def create_league(
    payload: LeagueCreateRequest,
    current_user: CurrentUser,
    uc: Annotated[CreateLeague, Depends(get_create_league)],
) -> LeagueResponse:
    league = await uc.execute(owner_id=current_user.id, name=payload.name)
    return LeagueResponse.from_domain(league, members=1)


@router.post(
    "/leagues/join",
    response_model=LeagueResponse,
    summary="Вступить в лигу по коду",
)
async def join_league(
    payload: LeagueJoinRequest,
    current_user: CurrentUser,
    uc: Annotated[JoinLeague, Depends(get_join_league)],
) -> LeagueResponse:
    league = await uc.execute(
        user_id=current_user.id, invite_code=payload.invite_code
    )
    return LeagueResponse.from_domain(league)


@router.get(
    "/leagues/mine",
    response_model=list[LeagueResponse],
    summary="Мои лиги",
)
async def my_leagues(
    current_user: CurrentUser,
    uc: Annotated[ListMyLeagues, Depends(get_list_my_leagues)],
) -> list[LeagueResponse]:
    items = await uc.execute(user_id=current_user.id)
    return [LeagueResponse.from_summary(s) for s in items]


@router.delete(
    "/leagues/{league_id}/leave",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Выйти из лиги",
)
async def leave_league(
    league_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[LeaveLeague, Depends(get_leave_league)],
) -> None:
    await uc.execute(user_id=current_user.id, league_id=league_id)


@router.get(
    "/leagues/{league_id}/standings",
    response_model=LeagueStandingsResponse,
    summary="Лидерборд лиги",
)
async def league_standings(
    league_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[GetLeagueStandings, Depends(get_league_standings)],
) -> LeagueStandingsResponse:
    result = await uc.execute(league_id=league_id, viewer_id=current_user.id)
    return LeagueStandingsResponse.from_result(result)


# ── Модерация лиг (admin) ────────────────────────────────────────────────────


@router.get(
    "/admin/leagues",
    response_model=LeagueListResponse,
    summary="Все приватные лиги (admin)",
)
async def list_all_leagues(
    _role: Annotated[object, Depends(require_admin)],
    uc: Annotated[ListAllLeagues, Depends(get_list_all_leagues)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LeagueListResponse:
    """Список для модерации: владелец видит только свои лиги, админ — все."""
    page = await uc.execute(limit=limit, offset=offset)
    return LeagueListResponse.from_page(page)


@router.patch(
    "/admin/leagues/{league_id}",
    response_model=LeagueResponse,
    summary="Переименовать лигу (admin)",
)
async def rename_league(
    league_id: uuid.UUID,
    payload: LeagueRenameRequest,
    current_user: CurrentUser,
    _role: Annotated[object, Depends(require_admin)],
    uc: Annotated[RenameLeague, Depends(get_rename_league)],
) -> LeagueResponse:
    """Модерация недопустимого названия; факт правки уходит в аудит."""
    league = await uc.execute(
        actor_id=current_user.id, league_id=league_id, name=payload.name
    )
    return LeagueResponse.from_domain(league)


@router.delete(
    "/admin/leagues/{league_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Удалить лигу (admin)",
)
async def delete_league(
    league_id: uuid.UUID,
    current_user: CurrentUser,
    _role: Annotated[object, Depends(require_admin)],
    uc: Annotated[DeleteLeague, Depends(get_delete_league)],
) -> None:
    """Сносит лигу вместе с участием.

    Обычное удаление, а не мягкое: лига не связана ни с прогнозами, ни с
    призовым зачётом, ни с деньгами. Сам факт остаётся в append-only аудите.
    """
    await uc.execute(actor_id=current_user.id, league_id=league_id)


# ── Дивизионы ────────────────────────────────────────────────────────────────


@router.get(
    "/seasons/{season_id}/divisions/{level}/standings",
    response_model=DivisionStandingsResponse,
    summary="Лидерборд дивизиона в сезоне",
)
async def division_standings(
    season_id: uuid.UUID,
    level: int,
    uc: Annotated[GetDivisionStandings, Depends(get_division_standings)],
) -> DivisionStandingsResponse:
    result = await uc.execute(season_id=season_id, level=level)
    return DivisionStandingsResponse.from_result(result)


@router.post(
    "/admin/divisions/seed",
    summary="Первичный посев дивизионов в сезоне (admin)",
)
async def seed_divisions(
    payload: SeedDivisionsRequest,
    _role: Annotated[object, Depends(require_admin)],
    uc: Annotated[SeedSeasonDivisions, Depends(get_seed_divisions)],
) -> dict[str, int]:
    """Раскладывает участников по дивизионам, когда прошлого сезона ещё нет.

    ``/admin/divisions/apply`` строит расстановку из membership завершённого
    сезона — для первого сезона брать неоткуда. Здесь берутся все активные
    аккаунты без дивизиона в этом сезоне. Повтор безопасен: уже назначенных не
    трогаем, пока не передан ``overwrite``.
    """
    written = await uc.execute(
        season_id=payload.season_id,
        even_split=payload.even_split,
        overwrite=payload.overwrite,
    )
    return {"placed": written}


@router.post(
    "/admin/divisions/apply",
    summary="Разнести дивизионы на следующий сезон (admin)",
)
async def apply_promotion(
    payload: ApplyPromotionRequest,
    _role: Annotated[object, Depends(require_admin)],
    uc: Annotated[ApplyPromotionRelegation, Depends(get_apply_promotion)],
) -> dict[str, int]:
    written = await uc.execute(
        finished_season_id=payload.finished_season_id,
        next_season_id=payload.next_season_id,
        promote=payload.promote,
        relegate=payload.relegate,
    )
    return {"placed": written}
