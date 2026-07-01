"""Роутер лиг и дивизионов."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.identity.api.dependencies import CurrentUser
from app.modules.leagues.api.dependencies import (
    get_apply_promotion,
    get_create_league,
    get_division_standings,
    get_join_league,
    get_league_standings,
    get_leave_league,
    get_list_my_leagues,
    require_admin,
)
from app.modules.leagues.api.schemas import (
    ApplyPromotionRequest,
    DivisionStandingsResponse,
    LeagueCreateRequest,
    LeagueJoinRequest,
    LeagueResponse,
    LeagueStandingsResponse,
)
from app.modules.leagues.application.use_cases import (
    ApplyPromotionRelegation,
    CreateLeague,
    GetDivisionStandings,
    GetLeagueStandings,
    JoinLeague,
    LeaveLeague,
    ListMyLeagues,
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
