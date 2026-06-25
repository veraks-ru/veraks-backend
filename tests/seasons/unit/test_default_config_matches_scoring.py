"""Гарантия согласованности: дефолт ``LeagueConfig`` совпадает с константами scoring.

Домен seasons не импортирует scoring (ацикличность), поэтому его нейтральный
дефолт задан литералами. Этот тест — единственное место, где оба домена
сводятся вместе, чтобы дефолтные правила сезона не разошлись с «боевыми»
константами скоринга при будущей правке одной из сторон.
"""

from __future__ import annotations

from app.modules.scoring.domain import constants
from app.modules.seasons.domain.value_objects import LeagueConfig


def test_default_league_config_mirrors_scoring_constants() -> None:
    cfg = LeagueConfig.default()
    assert cfg.n_min == constants.N_MIN
    assert cfg.c_min == constants.C_MIN
    assert cfg.w_min == constants.W_MIN
    assert cfg.k_shrink == constants.K_SHRINK
    assert cfg.min_predictors == constants.MIN_PREDICTORS
    # Стартовая сетка градаций совпадает с симметричной шкалой скоринга.
    assert cfg.gradation_map == (0.1, 0.3, 0.5, 0.7, 0.9)
    assert cfg.gradation_map[0] == constants.P_MIN
    assert cfg.gradation_map[-1] == constants.P_MAX
