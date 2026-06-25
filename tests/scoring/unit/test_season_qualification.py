"""Юнит-тесты квалификации сезона в ``RecomputeRatings`` (интеграция с seasons).

Проверяют: флаг ``qualified`` ставится по порогам замороженного ``LeagueConfig``;
два разных случая «конфиг недоступен» (нормальный пропуск vs ошибка инварианта);
global/category-области квалификацию не несут.
"""

from __future__ import annotations

import logging
import uuid

import pytest

from app.modules.scoring.application.dto import SeasonConfigView
from app.modules.scoring.application.use_cases import RecomputeRatings
from app.modules.scoring.domain.entities import ScopeType
from app.modules.seasons.domain.entities import SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig
from tests.scoring.conftest import FIXED_NOW, make_event
from tests.scoring.fakes import (
    FakeClock,
    FakeEventScoringGateway,
    FakeSeasonConfigGateway,
    InMemoryRatingRepository,
)

# Мягкий конфиг: один рейтинговый прогноз уже квалифицирует.
EASY_CFG = LeagueConfig(
    gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
    n_min=1,
    c_min=1,
    w_min=0.0,
    m_per_category=1,
    k_shrink=6.0,
    min_predictors=5,
)


def _season_event(season_id: uuid.UUID):
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        season_id=season_id,
        user_ids=ids,
    )
    return event, ids


async def _run(gateway, season_config) -> InMemoryRatingRepository:
    repo = InMemoryRatingRepository()
    await RecomputeRatings(
        gateway=gateway,
        ratings=repo,
        clock=FakeClock(FIXED_NOW),
        season_config=season_config,
    ).execute()
    return repo


async def test_qualified_flag_true_when_thresholds_met() -> None:
    season_id = uuid.uuid4()
    event, _ = _season_event(season_id)
    gateway = FakeEventScoringGateway(resolved=[event])
    season_config = FakeSeasonConfigGateway(
        configs={season_id: SeasonConfigView(status=SeasonStatus.ACTIVE, config=EASY_CFG)}
    )
    repo = await _run(gateway, season_config)

    season_board = await repo.leaderboard(ScopeType.SEASON, season_id)
    assert season_board, "ожидаем сезонные рейтинги"
    assert all(r.qualified is True for r in season_board)


async def test_qualified_flag_false_with_strict_default_config() -> None:
    season_id = uuid.uuid4()
    event, _ = _season_event(season_id)
    gateway = FakeEventScoringGateway(resolved=[event])
    season_config = FakeSeasonConfigGateway(
        configs={
            season_id: SeasonConfigView(
                status=SeasonStatus.ACTIVE, config=LeagueConfig.default()
            )
        }
    )
    repo = await _run(gateway, season_config)

    season_board = await repo.leaderboard(ScopeType.SEASON, season_id)
    # n=1 < n_min=30 → никто не квалифицирован.
    assert all(r.qualified is False for r in season_board)


async def test_global_and_category_scopes_carry_no_qualification() -> None:
    season_id = uuid.uuid4()
    category_id = uuid.uuid4()
    ids = [uuid.uuid4() for _ in range(5)]
    event, _ = make_event(
        outcome=0,
        probabilities=[0.9, 0.9, 0.9, 0.9, 0.3],
        season_id=season_id,
        category_id=category_id,
        user_ids=ids,
    )
    gateway = FakeEventScoringGateway(resolved=[event])
    season_config = FakeSeasonConfigGateway(
        configs={season_id: SeasonConfigView(status=SeasonStatus.ACTIVE, config=EASY_CFG)}
    )
    repo = await _run(gateway, season_config)

    glob = await repo.leaderboard(ScopeType.GLOBAL, None)
    cat = await repo.leaderboard(ScopeType.CATEGORY, category_id)
    assert all(r.qualified is None for r in glob)
    assert all(r.qualified is None for r in cat)


async def test_upcoming_season_without_config_is_normal_skip_no_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    season_id = uuid.uuid4()
    event, _ = _season_event(season_id)
    gateway = FakeEventScoringGateway(resolved=[event])
    # Сезон ещё не активирован: конфига нет — это норма, не ошибка.
    season_config = FakeSeasonConfigGateway(
        configs={
            season_id: SeasonConfigView(status=SeasonStatus.UPCOMING, config=None)
        }
    )
    with caplog.at_level(logging.ERROR):
        repo = await _run(gateway, season_config)

    season_board = await repo.leaderboard(ScopeType.SEASON, season_id)
    assert all(r.qualified is None for r in season_board)
    assert caplog.records == []  # никаких error-логов


async def test_active_season_missing_config_logs_error_not_silent(
    caplog: pytest.LogCaptureFixture,
) -> None:
    season_id = uuid.uuid4()
    event, _ = _season_event(season_id)
    gateway = FakeEventScoringGateway(resolved=[event])
    # Активный сезон без конфига — нарушение инварианта: громкий error, не тихий пропуск.
    season_config = FakeSeasonConfigGateway(
        configs={season_id: SeasonConfigView(status=SeasonStatus.ACTIVE, config=None)}
    )
    with caplog.at_level(logging.ERROR):
        repo = await _run(gateway, season_config)

    season_board = await repo.leaderboard(ScopeType.SEASON, season_id)
    assert all(r.qualified is None for r in season_board)
    assert any(rec.levelno == logging.ERROR for rec in caplog.records)
