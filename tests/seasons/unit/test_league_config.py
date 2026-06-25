"""Юнит-тесты value-object ``LeagueConfig`` — замороженного снапшота правил сезона."""

from __future__ import annotations

import pytest

from app.modules.seasons.domain.errors import InvalidSeasonDataError
from app.modules.seasons.domain.value_objects import LeagueConfig


def test_default_config_is_neutral_grid_and_mvp_thresholds() -> None:
    cfg = LeagueConfig.default()
    assert cfg.gradation_map == (0.1, 0.3, 0.5, 0.7, 0.9)
    assert cfg.n_min == 30
    assert cfg.c_min == 4
    assert cfg.w_min == 8.0
    assert cfg.m_per_category == 1
    assert cfg.k_shrink == 6.0
    assert cfg.min_predictors == 5


def test_round_trips_through_dict() -> None:
    cfg = LeagueConfig.default()
    assert LeagueConfig.from_dict(cfg.to_dict()) == cfg


def test_rejects_non_monotonic_gradation_map() -> None:
    with pytest.raises(InvalidSeasonDataError):
        LeagueConfig(
            gradation_map=(0.1, 0.5, 0.3, 0.7, 0.9),  # 0.5 > 0.3 — порядок нарушен
            n_min=30,
            c_min=4,
            w_min=8.0,
            m_per_category=1,
            k_shrink=6.0,
            min_predictors=5,
        )


def test_rejects_probabilities_out_of_open_interval() -> None:
    with pytest.raises(InvalidSeasonDataError):
        LeagueConfig(
            gradation_map=(0.0, 0.3, 0.5, 0.7, 1.0),  # 0 и 1 ломают log-loss
            n_min=30,
            c_min=4,
            w_min=8.0,
            m_per_category=1,
            k_shrink=6.0,
            min_predictors=5,
        )


def test_rejects_non_positive_shrinkage() -> None:
    with pytest.raises(InvalidSeasonDataError):
        LeagueConfig(
            gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
            n_min=30,
            c_min=4,
            w_min=8.0,
            m_per_category=1,
            k_shrink=0.0,
            min_predictors=5,
        )


def test_rejects_min_predictors_below_two() -> None:
    # leave-one-out консенсусу нужно как минимум 2 голоса.
    with pytest.raises(InvalidSeasonDataError):
        LeagueConfig(
            gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
            n_min=30,
            c_min=4,
            w_min=8.0,
            m_per_category=1,
            k_shrink=6.0,
            min_predictors=1,
        )


def test_is_frozen() -> None:
    cfg = LeagueConfig.default()
    with pytest.raises(AttributeError):
        cfg.n_min = 5  # type: ignore[misc]
