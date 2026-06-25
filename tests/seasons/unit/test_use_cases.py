"""Юнит-тесты use-cases домена seasons (с in-memory фейками портов)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from app.modules.identity.domain.entities import UserRole
from app.modules.seasons.application.use_cases import (
    ActivateSeason,
    CreateSeason,
    GetSeason,
    ListSeasons,
    UpdateSeason,
)
from app.modules.seasons.domain.entities import SeasonStatus
from app.modules.seasons.domain.errors import (
    InvalidSeasonDataError,
    InvalidSeasonTransitionError,
    SeasonNotFoundError,
    SeasonPermissionError,
    SeasonSlugTakenError,
)
from app.modules.seasons.domain.value_objects import LeagueConfig
from tests.seasons.fakes import FakeClock, InMemorySeasonRepository

NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
STARTS = datetime(2026, 7, 1, tzinfo=timezone.utc)
ENDS = datetime(2026, 9, 30, tzinfo=timezone.utc)


def _repo() -> InMemorySeasonRepository:
    return InMemorySeasonRepository()


def _clock() -> FakeClock:
    return FakeClock(NOW)


async def _make_season(repo: InMemorySeasonRepository, *, slug: str = "2026q3"):
    return await CreateSeason(repo=repo, clock=_clock()).execute(
        slug=slug,
        title="Сезон III",
        starts_at=STARTS,
        ends_at=ENDS,
        actor_role=UserRole.ADMIN,
    )


async def test_create_season_starts_upcoming_without_config() -> None:
    repo = _repo()
    season = await _make_season(repo)
    assert season.status is SeasonStatus.UPCOMING
    assert season.league_config is None
    assert await repo.get_by_slug("2026q3") is not None


async def test_create_rejects_non_manager_role() -> None:
    repo = _repo()
    with pytest.raises(SeasonPermissionError):
        await CreateSeason(repo=repo, clock=_clock()).execute(
            slug="x",
            title="X",
            starts_at=STARTS,
            ends_at=ENDS,
            actor_role=UserRole.USER,
        )


async def test_create_rejects_duplicate_slug() -> None:
    repo = _repo()
    await _make_season(repo)
    with pytest.raises(SeasonSlugTakenError):
        await _make_season(repo)


async def test_create_rejects_end_before_start() -> None:
    repo = _repo()
    with pytest.raises(InvalidSeasonDataError):
        await CreateSeason(repo=repo, clock=_clock()).execute(
            slug="bad",
            title="Bad",
            starts_at=ENDS,
            ends_at=STARTS,
            actor_role=UserRole.EDITOR,
        )


async def test_update_changes_fields_while_upcoming() -> None:
    repo = _repo()
    season = await _make_season(repo)
    updated = await UpdateSeason(repo=repo, clock=_clock()).execute(
        season_id=season.id, actor_role=UserRole.EDITOR, title="Новый титул"
    )
    assert updated.title == "Новый титул"


async def test_update_blocked_once_active() -> None:
    repo = _repo()
    season = await _make_season(repo)
    await ActivateSeason(repo=repo, clock=_clock()).execute(
        season_id=season.id, config=LeagueConfig.default(), actor_role=UserRole.ADMIN
    )
    with pytest.raises(InvalidSeasonTransitionError):
        await UpdateSeason(repo=repo, clock=_clock()).execute(
            season_id=season.id, actor_role=UserRole.EDITOR, title="Поздно"
        )


async def test_update_missing_season_raises() -> None:
    repo = _repo()
    with pytest.raises(SeasonNotFoundError):
        await UpdateSeason(repo=repo, clock=_clock()).execute(
            season_id=uuid.uuid4(), actor_role=UserRole.ADMIN, title="X"
        )


async def test_activate_snapshots_config_and_requires_admin() -> None:
    repo = _repo()
    season = await _make_season(repo)
    cfg = LeagueConfig.default()
    activated = await ActivateSeason(repo=repo, clock=_clock()).execute(
        season_id=season.id, config=cfg, actor_role=UserRole.ADMIN
    )
    assert activated.status is SeasonStatus.ACTIVE
    assert activated.league_config == cfg


async def test_activate_rejects_non_admin() -> None:
    repo = _repo()
    season = await _make_season(repo)
    with pytest.raises(SeasonPermissionError):
        await ActivateSeason(repo=repo, clock=_clock()).execute(
            season_id=season.id,
            config=LeagueConfig.default(),
            actor_role=UserRole.EDITOR,
        )


async def test_activate_is_idempotent() -> None:
    repo = _repo()
    season = await _make_season(repo)
    uc = ActivateSeason(repo=repo, clock=_clock())
    cfg = LeagueConfig.default()
    await uc.execute(season_id=season.id, config=cfg, actor_role=UserRole.ADMIN)
    again = await uc.execute(
        season_id=season.id, config=cfg, actor_role=UserRole.ADMIN
    )
    assert again.status is SeasonStatus.ACTIVE


async def test_list_filters_by_status() -> None:
    repo = _repo()
    s1 = await _make_season(repo, slug="a")
    await _make_season(repo, slug="b")
    await ActivateSeason(repo=repo, clock=_clock()).execute(
        season_id=s1.id, config=LeagueConfig.default(), actor_role=UserRole.ADMIN
    )
    active = await ListSeasons(repo=repo).execute(status=SeasonStatus.ACTIVE)
    assert [s.slug for s in active] == ["a"]


async def test_get_season_by_slug_or_not_found() -> None:
    repo = _repo()
    await _make_season(repo)
    found = await GetSeason(repo=repo).execute(slug="2026q3")
    assert found.slug == "2026q3"
    with pytest.raises(SeasonNotFoundError):
        await GetSeason(repo=repo).execute(slug="missing")
