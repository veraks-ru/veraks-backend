"""Composition root модуля leagues."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.leagues.adapters.repository import (
    SqlAlchemyDivisionMembershipRepository,
    SqlAlchemyDivisionRepository,
    SqlAlchemyLeagueMembershipRepository,
    SqlAlchemyLeagueRepository,
)
from app.modules.leagues.adapters.standings_gateway import (
    SqlAlchemyStandingsGateway,
)
from app.modules.leagues.adapters.user_lookup import SecretsInviteCodeGenerator
from app.modules.leagues.application.use_cases import (
    CreateLeague,
    GetDivisionStandings,
    GetLeagueStandings,
    JoinLeague,
    LeaveLeague,
    ListMyLeagues,
)

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_create_league(session: SessionDep) -> CreateLeague:
    return CreateLeague(
        leagues=SqlAlchemyLeagueRepository(session),
        memberships=SqlAlchemyLeagueMembershipRepository(session),
        codes=SecretsInviteCodeGenerator(),
    )


def get_join_league(session: SessionDep) -> JoinLeague:
    return JoinLeague(
        leagues=SqlAlchemyLeagueRepository(session),
        memberships=SqlAlchemyLeagueMembershipRepository(session),
    )


def get_leave_league(session: SessionDep) -> LeaveLeague:
    return LeaveLeague(memberships=SqlAlchemyLeagueMembershipRepository(session))


def get_list_my_leagues(session: SessionDep) -> ListMyLeagues:
    return ListMyLeagues(
        leagues=SqlAlchemyLeagueRepository(session),
        memberships=SqlAlchemyLeagueMembershipRepository(session),
    )


def get_league_standings(session: SessionDep) -> GetLeagueStandings:
    return GetLeagueStandings(
        leagues=SqlAlchemyLeagueRepository(session),
        memberships=SqlAlchemyLeagueMembershipRepository(session),
        standings=SqlAlchemyStandingsGateway(session),
    )


def get_division_standings(session: SessionDep) -> GetDivisionStandings:
    return GetDivisionStandings(
        divisions=SqlAlchemyDivisionRepository(session),
        memberships=SqlAlchemyDivisionMembershipRepository(session),
        standings=SqlAlchemyStandingsGateway(session),
    )
