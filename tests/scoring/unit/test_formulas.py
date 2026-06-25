"""Юнит-тесты чистой математики скоринга (домен ``scoring.domain.formulas``).

Каждый кейс — это воспроизведение конкретного численного примера из
спецификации скоринга: точность (Brier/log-loss), честность (properness),
консенсус толпы (leave-one-out), вес события по сложности, превышение над
толпой и усаженный сезонный рейтинг.
"""

from __future__ import annotations

import math

import pytest

from app.modules.scoring.domain.errors import NotEnoughPredictorsError
from app.modules.scoring.domain.formulas import (
    brier,
    crowd_advantage,
    consensus,
    disagreement,
    event_contribution,
    event_weight,
    expected_brier,
    leave_one_out_consensus,
    log_loss,
    qualifies,
    season_rating_from_contributions,
    surprise,
)

APPROX = 1e-4


# ── 1.1/1.2 Brier ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("probability", "outcome", "expected"),
    [
        (0.10, 1, 0.81),
        (0.10, 0, 0.01),
        (0.30, 1, 0.49),
        (0.30, 0, 0.09),
        (0.50, 1, 0.25),
        (0.50, 0, 0.25),
        (0.70, 1, 0.09),  # «Скорее да», событие произошло
        (0.70, 0, 0.49),
        (0.90, 1, 0.01),
        (0.90, 0, 0.81),
    ],
)
def test_brier_matches_specification_table(probability, outcome, expected) -> None:
    assert brier(probability, outcome) == pytest.approx(expected, abs=APPROX)


def test_brier_bounded_unit_interval() -> None:
    for p in (0.1, 0.3, 0.5, 0.7, 0.9):
        for o in (0, 1):
            assert 0.0 <= brier(p, o) <= 1.0


# ── 1.2 log-loss ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("probability", "outcome", "expected"),
    [
        (0.70, 1, 0.356675),  # −ln(0.7)
        (0.10, 1, 2.302585),  # −ln(0.1) — ограниченный максимум на крайней точке
        (0.90, 0, 2.302585),
        (0.50, 1, 0.693147),
        (0.50, 0, 0.693147),
    ],
)
def test_log_loss_matches_specification(probability, outcome, expected) -> None:
    assert log_loss(probability, outcome) == pytest.approx(expected, abs=APPROX)


# ── 1.3 Properness: честный отчёт — единственный оптимум ────────────────────


def test_expected_brier_minimized_by_honest_report() -> None:
    """E[BS] при истинной вере q=0.7: честный 0.7 строго лучше лжи и хеджа."""
    honest = expected_brier(0.7, 0.7)
    exaggerate = expected_brier(0.7, 0.9)
    hedge = expected_brier(0.7, 0.5)
    assert honest == pytest.approx(0.210, abs=APPROX)
    assert exaggerate == pytest.approx(0.250, abs=APPROX)
    assert hedge == pytest.approx(0.250, abs=APPROX)
    assert honest < exaggerate
    assert honest < hedge


# ── 2.1 Консенсус толпы и leave-one-out ─────────────────────────────────────


def test_consensus_is_mean() -> None:
    assert consensus([0.1, 0.3, 0.5, 0.7, 0.9]) == pytest.approx(0.5, abs=APPROX)


def test_loo_excludes_own_vote() -> None:
    # Толпа [0.9,0.9,0.9,0.9] + собственный голос 0.3 → бенчмарк без себя = 0.9.
    assert leave_one_out_consensus([0.9, 0.9, 0.9, 0.9, 0.3], 0.3) == pytest.approx(
        0.9, abs=APPROX
    )


def test_loo_requires_at_least_two_predictors() -> None:
    with pytest.raises(NotEnoughPredictorsError):
        leave_one_out_consensus([0.7], 0.7)


# ── 2.1 Превышение над толпой Δ ─────────────────────────────────────────────


def test_advantage_zero_on_trivial_consensus() -> None:
    # Очевидное: все 0.9, исход ДА, игрок как толпа → Δ = 0.
    assert crowd_advantage(0.9, [0.9, 0.9, 0.9], 1) == pytest.approx(0.0, abs=APPROX)


def test_advantage_large_when_right_in_surprise() -> None:
    # Неожиданный исход: толпа 0.9, исход НЕТ, игрок 0.3 → Δ = 0.81 − 0.09 = +0.72.
    probs = [0.9, 0.9, 0.9, 0.9, 0.3]
    assert crowd_advantage(0.3, probs, 0) == pytest.approx(0.72, abs=APPROX)


def test_advantage_penalizes_overconfidence_beyond_crowd() -> None:
    # Толпа 0.9, исход НЕТ, игрок 0.95 → Δ = 0.81 − 0.9025 = −0.0925.
    probs = [0.9, 0.9, 0.9, 0.9, 0.95]
    assert crowd_advantage(0.95, probs, 0) == pytest.approx(-0.0925, abs=APPROX)


def test_advantage_punishes_reckless_contrarian() -> None:
    # Толпа 0.9, исход ДА, игрок 0.1 → Δ = 0.01 − 0.81 = −0.80.
    probs = [0.9, 0.9, 0.9, 0.9, 0.1]
    assert crowd_advantage(0.1, probs, 1) == pytest.approx(-0.80, abs=APPROX)


# ── 2.2/2.3 Вес события по сложности ────────────────────────────────────────


def test_weight_trivial_event_near_zero() -> None:
    # Все 0.9, исход ДА → почти нулевой вес (≈0.03).
    assert event_weight([0.9, 0.9, 0.9], 1) == pytest.approx(0.03, abs=0.005)


def test_weight_unanimous_coinflip() -> None:
    # Все 0.5 → вес ≈0.20 (нет разногласия, но 1 бит неожиданности).
    assert event_weight([0.5, 0.5, 0.5], 1) == pytest.approx(0.20, abs=0.005)


def test_weight_split_crowd() -> None:
    # Раскол ½×0.1, ½×0.9, исход ДА → вес ≈0.55.
    probs = [0.1, 0.1, 0.9, 0.9]
    assert event_weight(probs, 1) == pytest.approx(0.55, abs=0.005)


def test_weight_confident_consensus_failed() -> None:
    # Все 0.9, исход НЕТ → максимальная неожиданность, вес ≈0.65.
    assert event_weight([0.9, 0.9, 0.9], 0) == pytest.approx(0.65, abs=0.005)


def test_weight_bounded_unit_interval() -> None:
    assert 0.0 <= event_weight([0.1, 0.9, 0.5], 1) <= 1.0


def test_disagreement_and_surprise_components_bounded() -> None:
    assert disagreement([0.1, 0.9]) == pytest.approx(1.0, abs=APPROX)  # макс. разброс
    assert disagreement([0.5, 0.5]) == pytest.approx(0.0, abs=APPROX)
    # Неожиданность исхода под консенсусом 0.5 = ровно 1 бит → нормировано.
    assert surprise(0.5, 1) == pytest.approx(1.0 / math.log2(10), abs=APPROX)


# ── 2.4 Вклад события π = w · Δ ─────────────────────────────────────────────


def test_event_contribution_combines_weight_and_advantage() -> None:
    probs = [0.9, 0.9, 0.9, 0.9, 0.3]  # игрок 0.3, исход НЕТ
    weight, contribution = event_contribution(0.3, probs, 0)
    # Консенсус с учётом голоса 0.3 = 0.78 → вес ≈0.637; Δ (по LOO=0.9) = 0.72.
    assert weight == pytest.approx(0.637, abs=0.005)
    assert contribution == pytest.approx(weight * 0.72, abs=0.005)


# ── 3.2 Сезонный рейтинг: усаженное средневзвешенное превышение ─────────────


def test_season_rating_newbie_single_lucky_shot() -> None:
    # Один трудный угаданный: Δ=0.72, w=0.70, k=6 → R = 0.504/6.70 ≈ 0.0752.
    rating = season_rating_from_contributions([0.70], [0.72], k=6.0)
    assert rating == pytest.approx(0.0752, abs=APPROX)


def test_season_rating_veteran_steady_edge() -> None:
    # Ровное преимущество 0.05/ед.веса, Σw=60, k=6 → R = 3.0/66 ≈ 0.04545.
    weights = [1.0] * 60
    advantages = [0.05] * 60
    rating = season_rating_from_contributions(weights, advantages, k=6.0)
    assert rating == pytest.approx(0.04545, abs=APPROX)


def test_season_rating_empty_is_zero() -> None:
    assert season_rating_from_contributions([], [], k=6.0) == pytest.approx(0.0)


def test_season_rating_farming_easy_events_is_neutral() -> None:
    # Лёгкие события (w≈0, Δ≈0) почти не двигают рейтинг.
    base = season_rating_from_contributions([0.70], [0.72], k=6.0)
    with_farm = season_rating_from_contributions(
        [0.70, 0.01, 0.01], [0.72, 0.0, 0.0], k=6.0
    )
    assert with_farm == pytest.approx(base, abs=0.01)


# ── 3.1 Пороги квалификации ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("n", "categories", "weight", "expected"),
    [
        (30, 4, 8.0, True),
        (29, 4, 8.0, False),  # мало прогнозов
        (30, 3, 8.0, False),  # мало категорий
        (30, 4, 7.9, False),  # недобор охвата сложности
        (100, 10, 50.0, True),
    ],
)
def test_qualifies_requires_all_three_thresholds(n, categories, weight, expected) -> None:
    assert qualifies(n, categories, weight) is expected
