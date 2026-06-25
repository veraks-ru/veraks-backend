"""Composition root модуля seasons (FastAPI DI).

Здесь — и только здесь — конкретные адаптеры связываются с портами и
собираются use-cases. В тестах достаточно переопределить провайдеры портов.

Намеренно **не импортирует** домен scoring (направление зависимостей —
``scoring → seasons``). Дефолт ``LeagueConfig`` для активации — собственный
нейтральный fallback seasons.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.seasons.adapters.clock import SystemClock
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.modules.seasons.application.use_cases import (
    ActivateSeason,
    CreateSeason,
    GetSeason,
    ListSeasons,
    UpdateSeason,
)
from app.modules.seasons.ports.clock import Clock
from app.modules.seasons.ports.repositories import SeasonRepository

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_clock() -> Clock:
    """Серверные часы (переопределяются в тестах фиксированными)."""
    return SystemClock()


ClockDep = Annotated[Clock, Depends(get_clock)]


def get_season_repository(session: SessionDep) -> SeasonRepository:
    """Репозиторий сезонов."""
    return SqlAlchemySeasonRepository(session)


SeasonRepoDep = Annotated[SeasonRepository, Depends(get_season_repository)]


def get_create_season(repo: SeasonRepoDep, clock: ClockDep) -> CreateSeason:
    return CreateSeason(repo=repo, clock=clock)


def get_update_season(repo: SeasonRepoDep, clock: ClockDep) -> UpdateSeason:
    return UpdateSeason(repo=repo, clock=clock)


def get_activate_season(repo: SeasonRepoDep, clock: ClockDep) -> ActivateSeason:
    return ActivateSeason(repo=repo, clock=clock)


def get_list_seasons(repo: SeasonRepoDep) -> ListSeasons:
    return ListSeasons(repo=repo)


def get_get_season(repo: SeasonRepoDep) -> GetSeason:
    return GetSeason(repo=repo)
