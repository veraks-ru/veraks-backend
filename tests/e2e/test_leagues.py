"""E2E лиг и дивизионов против реального Postgres."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

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
    ApplyPromotionRelegation,
    CreateLeague,
    GetDivisionStandings,
    GetLeagueStandings,
    JoinLeague,
    LeaveLeague,
)
from app.modules.leagues.domain.entities import DivisionMembership
from app.modules.seasons.adapters.season_repository import SqlAlchemySeasonRepository
from app.modules.seasons.domain.entities import Season, SeasonStatus
from tests.e2e.helpers import add_user

pytestmark = pytest.mark.asyncio
UTC = timezone.utc


async def _insert_rating(
    session, *, user_id, scope, scope_id, skill, rank
):  # noqa: ANN001
    await session.execute(
        text(
            "INSERT INTO ratings "
            "(id, user_id, scope_type, scope_id, mean_brier, skill_score, "
            " calibration_error, n_resolved, rank, updated_at) "
            "VALUES (gen_random_uuid(), :uid, CAST(:scope AS rating_scope), "
            " :sid, 0.10000, :skill, 0.10000, 5, :rank, now())"
        ),
        {
            "uid": str(user_id),
            "scope": scope,
            "sid": str(scope_id) if scope_id else None,
            "skill": skill,
            "rank": rank,
        },
    )


# ── Приватные лиги ───────────────────────────────────────────────────────────


async def test_private_league_create_join_standings_leave(
    session: AsyncSession,
) -> None:
    owner = await add_user(session, username="owner1")
    b = await add_user(session, username="memb_b")
    c = await add_user(session, username="memb_c")
    await session.flush()
    # Глобальные рейтинги задают порядок в лидерборде лиги.
    await _insert_rating(session, user_id=owner.id, scope="global", scope_id=None, skill=0.30000, rank=1)
    await _insert_rating(session, user_id=b.id, scope="global", scope_id=None, skill=0.10000, rank=3)
    await _insert_rating(session, user_id=c.id, scope="global", scope_id=None, skill=0.20000, rank=2)
    await session.flush()

    leagues = SqlAlchemyLeagueRepository(session)
    members = SqlAlchemyLeagueMembershipRepository(session)
    gateway = SqlAlchemyStandingsGateway(session)

    league = await CreateLeague(
        leagues=leagues, memberships=members, codes=SecretsInviteCodeGenerator()
    ).execute(owner_id=owner.id, name="  Клуб оракулов  ")
    assert league.name == "Клуб оракулов"

    join = JoinLeague(leagues=leagues, memberships=members)
    await join.execute(user_id=b.id, invite_code=league.invite_code)
    await join.execute(user_id=c.id, invite_code=league.invite_code)
    # Идемпотентность вступления.
    await join.execute(user_id=c.id, invite_code=league.invite_code)

    standings = await GetLeagueStandings(
        leagues=leagues, memberships=members, standings=gateway
    ).execute(league_id=league.id, viewer_id=b.id)
    assert standings.is_member is True
    names = [r.username for r in standings.rows]
    assert names == ["owner1", "memb_c", "memb_b"]  # по skill_score убыв.
    assert [r.rank for r in standings.rows] == [1, 2, 3]

    # Выход участника.
    await LeaveLeague(memberships=members).execute(
        user_id=c.id, league_id=league.id
    )
    after = await GetLeagueStandings(
        leagues=leagues, memberships=members, standings=gateway
    ).execute(league_id=league.id, viewer_id=owner.id)
    assert [r.username for r in after.rows] == ["owner1", "memb_b"]
    await session.commit()


# ── Дивизионы ────────────────────────────────────────────────────────────────


def _season(slug: str, status: SeasonStatus) -> Season:
    return Season(
        slug=slug,
        title=slug,
        starts_at=datetime(2026, 1, 1, tzinfo=UTC),
        ends_at=datetime(2026, 3, 31, tzinfo=UTC),
        status=status,
    )


async def test_division_promotion_relegation(session: AsyncSession) -> None:
    divisions = SqlAlchemyDivisionRepository(session)
    dmembers = SqlAlchemyDivisionMembershipRepository(session)
    gateway = SqlAlchemyStandingsGateway(session)
    seasons = SqlAlchemySeasonRepository(session)

    d1 = await divisions.get_by_level(1)
    d2 = await divisions.get_by_level(2)
    d3 = await divisions.get_by_level(3)
    assert d1 and d2 and d3  # засеяны миграцией 0016

    finished = _season("2026q1", SeasonStatus.FINISHED)
    upcoming = _season("2026q2", SeasonStatus.UPCOMING)
    await seasons.add(finished)
    await seasons.add(upcoming)
    await session.flush()

    # Раскладка по дивизионам в завершённом сезоне + сезонные рейтинги (rank).
    layout = {
        d1.id: ["d1a", "d1b", "d1c"],
        d2.id: ["d2a", "d2b", "d2c"],
        d3.id: ["d3a", "d3b"],
    }
    users: dict[str, object] = {}
    rank = 1
    for div_id, handles in layout.items():
        for h in handles:
            u = await add_user(session, username=h)
            users[h] = u
            await dmembers.upsert(
                DivisionMembership(
                    user_id=u.id, season_id=finished.id, division_id=div_id
                )
            )
            await _insert_rating(
                session,
                user_id=u.id,
                scope="season",
                scope_id=finished.id,
                skill=0.50000,
                rank=rank,
            )
            rank += 1
    await session.flush()

    written = await ApplyPromotionRelegation(
        divisions=divisions, memberships=dmembers, standings=gateway
    ).execute(
        finished_season_id=finished.id,
        next_season_id=upcoming.id,
        promote=1,
        relegate=1,
    )
    assert written == 8

    async def level_of(handle: str) -> int:
        m = await dmembers.get_for_user_season(users[handle].id, upcoming.id)  # type: ignore[attr-defined]
        assert m is not None
        div = await divisions.get_by_id(m.division_id)
        return div.level  # type: ignore[union-attr]

    # Высший: топ остаётся, низ падает.
    assert await level_of("d1a") == 1
    assert await level_of("d1c") == 2
    # Средний: топ поднимается, низ падает, середина остаётся.
    assert await level_of("d2a") == 1
    assert await level_of("d2b") == 2
    assert await level_of("d2c") == 3
    # Низший: топ поднимается, низ остаётся.
    assert await level_of("d3a") == 2
    assert await level_of("d3b") == 3

    # Лидерборд дивизиона (завершённый сезон, уровень 2).
    standings = await GetDivisionStandings(
        divisions=divisions, memberships=dmembers, standings=gateway
    ).execute(season_id=finished.id, level=2)
    assert standings.division_level == 2
    assert {r.username for r in standings.rows} == {"d2a", "d2b", "d2c"}
    await session.commit()
