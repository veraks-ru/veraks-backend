"""Юнит-тесты use-cases лиг: админская модерация и первичный посев дивизионов."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.modules.leagues.application.use_cases import (
    ApplyPromotionRelegation,
    DeleteLeague,
    ListAllLeagues,
    RenameLeague,
    SeedSeasonDivisions,
)
from app.modules.leagues.domain.entities import League, LeagueMembership
from app.modules.leagues.domain.errors import (
    InvalidLeagueDataError,
    LeagueNotFoundError,
)
from tests.leagues.fakes import (
    FakeAuditTrail,
    FakeStandingsGateway,
    FakeUserLookup,
    InMemoryDivisionMembershipRepository,
    InMemoryDivisionRepository,
    InMemoryLeagueMembershipRepository,
    InMemoryLeagueRepository,
)

FIXED_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


def _league(name: str, *, age_days: int = 0) -> League:
    return League(
        name=name,
        owner_id=uuid.uuid4(),
        invite_code=f"code{name}",
        created_at=FIXED_NOW - timedelta(days=age_days),
    )


# ── Список всех лиг (admin) ──────────────────────────────────────────────────


async def test_list_all_leagues_newest_first_with_member_counts() -> None:
    leagues = InMemoryLeagueRepository()
    memberships = InMemoryLeagueMembershipRepository()
    old = leagues.seed(_league("Старая", age_days=10))
    fresh = leagues.seed(_league("Новая", age_days=1))
    await memberships.add(LeagueMembership(league_id=fresh.id, user_id=uuid.uuid4()))
    await memberships.add(LeagueMembership(league_id=fresh.id, user_id=uuid.uuid4()))

    page = await ListAllLeagues(leagues=leagues, memberships=memberships).execute()

    assert [s.league.id for s in page.items] == [fresh.id, old.id]
    assert page.items[0].members == 2
    assert page.items[1].members == 0
    assert page.total == 2


async def test_list_all_leagues_paginates() -> None:
    leagues = InMemoryLeagueRepository()
    for i in range(5):
        leagues.seed(_league(f"Лига {i}", age_days=i))
    uc = ListAllLeagues(
        leagues=leagues, memberships=InMemoryLeagueMembershipRepository()
    )

    page = await uc.execute(limit=2, offset=2)

    assert len(page.items) == 2
    # total — по всей выборке, а не по странице (нужно для пагинации в UI).
    assert page.total == 5


# ── Переименование (admin) ───────────────────────────────────────────────────


async def test_rename_league_writes_audit_diff() -> None:
    leagues = InMemoryLeagueRepository()
    audit = FakeAuditTrail()
    league = leagues.seed(_league("Плохое название"))
    admin_id = uuid.uuid4()

    renamed = await RenameLeague(leagues=leagues, audit=audit).execute(
        actor_id=admin_id, league_id=league.id, name="  Нормальное  "
    )

    assert renamed.name == "Нормальное"  # обрезка пробелов как при создании
    assert audit.actions() == ["league.renamed"]
    record = audit.records[0]
    assert record["before"] == {"name": "Плохое название"}
    assert record["after"] == {"name": "Нормальное"}
    assert record["actor_id"] == admin_id


async def test_rename_league_rejects_empty_name() -> None:
    leagues = InMemoryLeagueRepository()
    audit = FakeAuditTrail()
    league = leagues.seed(_league("Лига"))

    with pytest.raises(InvalidLeagueDataError):
        await RenameLeague(leagues=leagues, audit=audit).execute(
            actor_id=uuid.uuid4(), league_id=league.id, name="   "
        )
    # Отказ до записи: аудит пуст, имя не тронуто.
    assert audit.records == []
    assert (await leagues.get_by_id(league.id)).name == "Лига"  # type: ignore[union-attr]


async def test_rename_unknown_league() -> None:
    with pytest.raises(LeagueNotFoundError):
        await RenameLeague(
            leagues=InMemoryLeagueRepository(), audit=FakeAuditTrail()
        ).execute(actor_id=uuid.uuid4(), league_id=uuid.uuid4(), name="Новая")


# ── Удаление (admin) ─────────────────────────────────────────────────────────


async def test_delete_league_removes_and_audits() -> None:
    leagues = InMemoryLeagueRepository()
    audit = FakeAuditTrail()
    league = leagues.seed(_league("На удаление"))

    await DeleteLeague(leagues=leagues, audit=audit).execute(
        actor_id=uuid.uuid4(), league_id=league.id
    )

    assert await leagues.get_by_id(league.id) is None
    assert audit.actions() == ["league.deleted"]
    # Имя владельца остаётся в append-only аудите — след не теряется.
    assert audit.records[0]["before"]["name"] == "На удаление"


async def test_delete_unknown_league() -> None:
    with pytest.raises(LeagueNotFoundError):
        await DeleteLeague(
            leagues=InMemoryLeagueRepository(), audit=FakeAuditTrail()
        ).execute(actor_id=uuid.uuid4(), league_id=uuid.uuid4())


# ── Первичный посев дивизионов ───────────────────────────────────────────────


async def test_seed_divisions_cold_start_places_everyone_in_lowest() -> None:
    season_id = uuid.uuid4()
    users = [uuid.uuid4() for _ in range(4)]
    divisions = InMemoryDivisionRepository(levels=3)
    memberships = InMemoryDivisionMembershipRepository()

    placed = await SeedSeasonDivisions(
        divisions=divisions,
        memberships=memberships,
        standings=FakeStandingsGateway(),
        users=FakeUserLookup(users),
    ).execute(season_id=season_id)

    assert placed == 4
    lowest = await divisions.get_by_level(3)
    assert lowest is not None
    assert all(m.division_id == lowest.id for m in memberships.rows.values())


async def test_seed_divisions_even_split_orders_by_global_rating() -> None:
    season_id = uuid.uuid4()
    strong, mid, weak = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    divisions = InMemoryDivisionRepository(levels=3)
    memberships = InMemoryDivisionMembershipRepository()
    standings = FakeStandingsGateway(
        {strong: Decimal("0.30"), mid: Decimal("0.10"), weak: Decimal("-0.05")}
    )

    await SeedSeasonDivisions(
        divisions=divisions,
        memberships=memberships,
        standings=standings,
        # Порядок на входе намеренно обратный — сортировать должен use-case.
        users=FakeUserLookup([weak, mid, strong]),
    ).execute(season_id=season_id, even_split=True)

    async def level_of(user_id: uuid.UUID) -> int:
        membership = memberships.rows[(user_id, season_id)]
        division = await divisions.get_by_id(membership.division_id)
        assert division is not None
        return division.level

    assert await level_of(strong) == 1
    assert await level_of(mid) == 2
    assert await level_of(weak) == 3


async def test_seed_divisions_skips_already_placed() -> None:
    """Повтор не затирает результаты промоции — в этом смысл идемпотентности."""
    season_id = uuid.uuid4()
    veteran, newcomer = uuid.uuid4(), uuid.uuid4()
    divisions = InMemoryDivisionRepository(levels=3)
    memberships = InMemoryDivisionMembershipRepository()
    top = await divisions.get_by_level(1)
    assert top is not None
    from app.modules.leagues.domain.entities import DivisionMembership

    await memberships.upsert(
        DivisionMembership(
            user_id=veteran, season_id=season_id, division_id=top.id
        )
    )

    placed = await SeedSeasonDivisions(
        divisions=divisions,
        memberships=memberships,
        standings=FakeStandingsGateway(),
        users=FakeUserLookup([veteran, newcomer]),
    ).execute(season_id=season_id)

    assert placed == 1  # только новичок
    assert memberships.rows[(veteran, season_id)].division_id == top.id


async def test_seed_divisions_overwrite_replaces_existing() -> None:
    season_id = uuid.uuid4()
    user = uuid.uuid4()
    divisions = InMemoryDivisionRepository(levels=3)
    memberships = InMemoryDivisionMembershipRepository()
    top = await divisions.get_by_level(1)
    lowest = await divisions.get_by_level(3)
    assert top is not None and lowest is not None
    from app.modules.leagues.domain.entities import DivisionMembership

    await memberships.upsert(
        DivisionMembership(user_id=user, season_id=season_id, division_id=top.id)
    )

    placed = await SeedSeasonDivisions(
        divisions=divisions,
        memberships=memberships,
        standings=FakeStandingsGateway(),
        users=FakeUserLookup([user]),
    ).execute(season_id=season_id, overwrite=True)

    assert placed == 1
    assert memberships.rows[(user, season_id)].division_id == lowest.id


async def test_apply_promotion_returns_zero_without_predecessor() -> None:
    """Регрессия на причину появления посева: промоция без прошлого сезона пуста."""
    divisions = InMemoryDivisionRepository(levels=3)
    memberships = InMemoryDivisionMembershipRepository()

    placed = await ApplyPromotionRelegation(
        divisions=divisions,
        memberships=memberships,
        standings=FakeStandingsGateway(),
    ).execute(finished_season_id=uuid.uuid4(), next_season_id=uuid.uuid4())

    assert placed == 0
    assert memberships.rows == {}
