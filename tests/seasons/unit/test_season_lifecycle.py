"""Юнит-тесты жизненного цикла сезона: переходы, идемпотентность, снапшот конфига."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.errors import InvalidSeasonTransitionError
from app.modules.seasons.domain.value_objects import LeagueConfig

NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 6, 25, 13, 0, tzinfo=timezone.utc)


def _season(status: SeasonStatus = SeasonStatus.UPCOMING) -> Season:
    return Season(
        slug="2026q3",
        title="Сезон III 2026",
        starts_at=NOW,
        ends_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
        status=status,
    )


def test_new_season_is_upcoming_without_config() -> None:
    season = _season()
    assert season.status is SeasonStatus.UPCOMING
    assert season.league_config is None


def test_activate_snapshots_passed_config_and_moves_to_active() -> None:
    season = _season()
    cfg = LeagueConfig.default()
    changed = season.activate(cfg, now=NOW)
    assert changed is True
    assert season.status is SeasonStatus.ACTIVE
    assert season.league_config == cfg
    assert season.updated_at == NOW


def test_activate_is_idempotent_noop_when_already_active() -> None:
    season = _season(SeasonStatus.ACTIVE)
    season.league_config = LeagueConfig.default()
    other = LeagueConfig(
        gradation_map=(0.2, 0.4, 0.5, 0.6, 0.8),
        n_min=10,
        c_min=2,
        w_min=4.0,
        m_per_category=1,
        k_shrink=3.0,
        min_predictors=3,
    )
    changed = season.activate(other, now=LATER)
    # No-op: статус и конфиг не перезаписываются (правила сезона неизменны).
    assert changed is False
    assert season.status is SeasonStatus.ACTIVE
    assert season.league_config == LeagueConfig.default()


def test_finalize_moves_active_to_finished() -> None:
    season = _season(SeasonStatus.ACTIVE)
    changed = season.finalize(now=LATER)
    assert changed is True
    assert season.status is SeasonStatus.FINISHED
    assert season.updated_at == LATER


def test_finalize_already_finished_is_noop_not_recompute() -> None:
    season = _season(SeasonStatus.FINISHED)
    changed = season.finalize(now=LATER)
    assert changed is False
    assert season.status is SeasonStatus.FINISHED


def test_cannot_finalize_upcoming_season() -> None:
    season = _season(SeasonStatus.UPCOMING)
    with pytest.raises(InvalidSeasonTransitionError):
        season.finalize(now=NOW)


def test_cannot_activate_finished_season() -> None:
    season = _season(SeasonStatus.FINISHED)
    with pytest.raises(InvalidSeasonTransitionError):
        season.activate(LeagueConfig.default(), now=NOW)
