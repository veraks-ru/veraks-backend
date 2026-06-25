"""Юнит-тесты чистой политики квалификации к призам (объём/разнообразие/охват)."""

from __future__ import annotations

from app.modules.seasons.domain.qualification import evaluate_qualification
from app.modules.seasons.domain.value_objects import LeagueConfig

CFG = LeagueConfig.default()  # n_min=30, c_min=4, w_min=8.0


def test_qualifies_when_all_three_thresholds_met() -> None:
    result = evaluate_qualification(
        n_resolved=30, category_count=4, total_weight=8.0, cfg=CFG
    )
    assert result.qualified is True
    assert result.volume_ok and result.diversity_ok and result.coverage_ok


def test_fails_volume_blocks_qualification() -> None:
    result = evaluate_qualification(
        n_resolved=29, category_count=4, total_weight=8.0, cfg=CFG
    )
    assert result.qualified is False
    assert result.volume_ok is False
    assert result.diversity_ok is True
    assert result.coverage_ok is True


def test_fails_diversity_blocks_qualification() -> None:
    result = evaluate_qualification(
        n_resolved=30, category_count=3, total_weight=8.0, cfg=CFG
    )
    assert result.qualified is False
    assert result.diversity_ok is False


def test_fails_coverage_blocks_qualification() -> None:
    # охват сложности недостижим фармом лёгких событий (w_e ≈ 0).
    result = evaluate_qualification(
        n_resolved=30, category_count=4, total_weight=7.99, cfg=CFG
    )
    assert result.qualified is False
    assert result.coverage_ok is False


def test_result_carries_observed_values_and_thresholds_for_why_not() -> None:
    result = evaluate_qualification(
        n_resolved=12, category_count=2, total_weight=3.5, cfg=CFG
    )
    assert result.n_resolved == 12
    assert result.category_count == 2
    assert result.total_weight == 3.5
    assert result.n_min == CFG.n_min
    assert result.c_min == CFG.c_min
    assert result.w_min == CFG.w_min
