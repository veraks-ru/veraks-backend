"""Composition root модуля leagues."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.identity.api.dependencies import CurrentUser
from app.modules.identity.domain.entities import UserRole
from app.modules.leagues.adapters.repository import (
    SqlAlchemyDivisionMembershipRepository,
    SqlAlchemyDivisionRepository,
    SqlAlchemyLeagueMembershipRepository,
    SqlAlchemyLeagueRepository,
)
from app.modules.leagues.adapters.standings_gateway import (
    SqlAlchemyStandingsGateway,
)
from app.modules.leagues.adapters.user_lookup import (
    SecretsInviteCodeGenerator,
    SqlAlchemyUserLookup,
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
from app.modules.leagues.domain.errors import LeaguePermissionError
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def require_admin(current_user: CurrentUser) -> UserRole:
    """Гард: операция только для администратора (разнос дивизионов)."""
    if current_user.role is not UserRole.ADMIN:
        raise LeaguePermissionError("Требуются права администратора")
    return current_user.role


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


def get_list_all_leagues(session: SessionDep) -> ListAllLeagues:
    return ListAllLeagues(
        leagues=SqlAlchemyLeagueRepository(session),
        memberships=SqlAlchemyLeagueMembershipRepository(session),
    )


def get_rename_league(session: SessionDep) -> RenameLeague:
    return RenameLeague(
        leagues=SqlAlchemyLeagueRepository(session),
        audit=SqlAlchemyAuditTrail(session),
    )


def get_delete_league(session: SessionDep) -> DeleteLeague:
    return DeleteLeague(
        leagues=SqlAlchemyLeagueRepository(session),
        audit=SqlAlchemyAuditTrail(session),
    )


def get_seed_divisions(session: SessionDep) -> SeedSeasonDivisions:
    return SeedSeasonDivisions(
        divisions=SqlAlchemyDivisionRepository(session),
        memberships=SqlAlchemyDivisionMembershipRepository(session),
        standings=SqlAlchemyStandingsGateway(session),
        users=SqlAlchemyUserLookup(session),
    )


def get_apply_promotion(session: SessionDep) -> ApplyPromotionRelegation:
    return ApplyPromotionRelegation(
        divisions=SqlAlchemyDivisionRepository(session),
        memberships=SqlAlchemyDivisionMembershipRepository(session),
        standings=SqlAlchemyStandingsGateway(session),
    )
