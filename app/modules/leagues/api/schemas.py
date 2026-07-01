"""Pydantic-схемы лиг и дивизионов."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.leagues.application.use_cases import (
    DivisionStandings,
    LeagueStandings,
    LeagueSummary,
)
from app.modules.leagues.domain.entities import League
from app.modules.leagues.ports.repositories import StandingRow


class LeagueCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class LeagueJoinRequest(BaseModel):
    invite_code: str = Field(min_length=1, max_length=32)


class LeagueResponse(BaseModel):
    id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    invite_code: str
    created_at: datetime
    members: int | None = None

    @classmethod
    def from_domain(cls, x: League, *, members: int | None = None) -> "LeagueResponse":
        return cls(
            id=x.id,
            name=x.name,
            owner_id=x.owner_id,
            invite_code=x.invite_code,
            created_at=x.created_at,
            members=members,
        )

    @classmethod
    def from_summary(cls, s: LeagueSummary) -> "LeagueResponse":
        return cls.from_domain(s.league, members=s.members)


class StandingRowResponse(BaseModel):
    rank: int
    user_id: uuid.UUID
    username: str
    display_name: str
    skill_score: str | None
    mean_brier: str | None
    n_resolved: int

    @classmethod
    def from_row(cls, r: StandingRow) -> "StandingRowResponse":
        return cls(
            rank=r.rank,
            user_id=r.user_id,
            username=r.username,
            display_name=r.display_name,
            skill_score=str(r.skill_score) if r.skill_score is not None else None,
            mean_brier=str(r.mean_brier) if r.mean_brier is not None else None,
            n_resolved=r.n_resolved,
        )


class LeagueStandingsResponse(BaseModel):
    league: LeagueResponse
    is_member: bool
    rows: list[StandingRowResponse]

    @classmethod
    def from_result(cls, x: LeagueStandings) -> "LeagueStandingsResponse":
        return cls(
            league=LeagueResponse.from_domain(x.league),
            is_member=x.is_member,
            rows=[StandingRowResponse.from_row(r) for r in x.rows],
        )


class DivisionStandingsResponse(BaseModel):
    level: int
    title: str
    season_id: uuid.UUID
    rows: list[StandingRowResponse]

    @classmethod
    def from_result(cls, x: DivisionStandings) -> "DivisionStandingsResponse":
        return cls(
            level=x.division_level,
            title=x.division_title,
            season_id=x.season_id,
            rows=[StandingRowResponse.from_row(r) for r in x.rows],
        )
