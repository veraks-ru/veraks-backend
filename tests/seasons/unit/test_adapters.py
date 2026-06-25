"""Юнит-тесты адаптеров seasons, не требующих БД: маппинг ORM."""

from __future__ import annotations

from datetime import datetime, timezone

from app.modules.seasons.adapters.orm import SeasonORM
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig

NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)


def test_season_orm_round_trip_active_with_config() -> None:
    season = Season(
        slug="2026q3",
        title="Сезон III",
        starts_at=NOW,
        ends_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        status=SeasonStatus.ACTIVE,
        league_config=LeagueConfig.default(),
        created_at=NOW,
        updated_at=NOW,
    )
    restored = SeasonORM.from_domain(season).to_domain()
    assert restored == season
    assert restored.league_config == LeagueConfig.default()


def test_season_orm_round_trip_upcoming_without_config() -> None:
    season = Season(
        slug="2026q4",
        title="Сезон IV",
        starts_at=NOW,
        ends_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        status=SeasonStatus.UPCOMING,
        created_at=NOW,
        updated_at=NOW,
    )
    restored = SeasonORM.from_domain(season).to_domain()
    assert restored.league_config is None
    assert restored == season
