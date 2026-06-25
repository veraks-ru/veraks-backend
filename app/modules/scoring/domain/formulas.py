"""Чистая математика скоринга — детерминированные функции без I/O.

Это ядро домена: точность (Brier/log-loss), честность (properness через
``expected_brier``), консенсус толпы и leave-one-out бенчмарк, вес события по
сложности и усаженный сезонный рейтинг. Функции тотальны на допустимом входе,
типизированы и юнит-тестируются в изоляции — никакого знания о БД, FastAPI или
доменных сущностях здесь нет.

Все вероятности — ``float`` в ``(0, 1)``; исход ``outcome`` — ``int ∈ {0, 1}``
(1 = исход «ДА» наступил). Конвертация из ``Decimal``/``bool`` происходит на
границе (прикладной слой/адаптеры), чтобы домен оставался простым и быстрым.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.modules.scoring.domain.constants import (
    BETA,
    C_MIN,
    DISP_NORM,
    K_SHRINK,
    N_MIN,
    SURPRISE_NORM,
    W_MIN,
)
from app.modules.scoring.domain.errors import NotEnoughPredictorsError

# Защита от ``log(0)`` в log-loss: вероятности не достигают 0/1, но клампим
# на всякий случай (крайние точки шкалы и так ограничены 0.1/0.9).
_EPS = 1e-9


# ── 1. Точность ─────────────────────────────────────────────────────────────


def brier(probability: float, outcome: int) -> float:
    """Brier score ``(p − o)²`` — основная метрика точности, диапазон ``[0, 1]``.

    Строго proper: ожидаемый балл минимизируется единственно при честном
    отчёте истинной веры. Меньше — лучше.
    """
    return (probability - outcome) ** 2


def log_loss(probability: float, outcome: int) -> float:
    """Логарифмическая функция потерь — вторичная метрика (B2B/аналитика).

    ``−[o·ln(p) + (1−o)·ln(1−p)]``; диапазон ``[0, ∞)``, ограничен крайними
    точками шкалы. Тоже строго proper.
    """
    p = min(max(probability, _EPS), 1.0 - _EPS)
    return -(outcome * math.log(p) + (1 - outcome) * math.log(1.0 - p))


def expected_brier(belief: float, reported: float) -> float:
    """Ожидаемый Brier при истинной вере ``belief`` и сообщённой ``reported``.

    ``q·(p−1)² + (1−q)·p²``. Минимум по ``reported`` — ровно в ``belief``
    (демонстрация properness): преувеличивать уверенность или хеджировать —
    всегда хуже честного отчёта в матожидании.
    """
    return belief * (reported - 1.0) ** 2 + (1.0 - belief) * reported**2


# ── 2.1 Консенсус толпы и leave-one-out ─────────────────────────────────────


def consensus(probabilities: Sequence[float]) -> float:
    """Консенсус толпы ``c_e`` — среднее всех прогнозов по событию.

    Поднимает :class:`ValueError` на пустом входе (нет толпы — нет консенсуса).
    """
    if not probabilities:
        raise ValueError("Консенсус не определён для пустого множества прогнозов")
    return math.fsum(probabilities) / len(probabilities)


def leave_one_out_consensus(probabilities: Sequence[float], own: float) -> float:
    """Консенсус толпы без собственного голоса игрока ``c_e^{LOO}``.

    ``(Σ p_i − p_u) / (n − 1)``. Исключение своего голоса делает бенчмарк
    негеймимым: нельзя «надуть» свой ориентир. Требует ≥ 2 предсказателей —
    иначе бенчмарка не существует (:class:`NotEnoughPredictorsError`).
    """
    n = len(probabilities)
    if n < 2:
        raise NotEnoughPredictorsError(
            "Для leave-one-out бенчмарка нужно минимум 2 предсказателя"
        )
    return (math.fsum(probabilities) - own) / (n - 1)


# ── 2.2 Компоненты веса события ─────────────────────────────────────────────


def disagreement(probabilities: Sequence[float]) -> float:
    """Нормированный разброс мнений ``ĝ_e ∈ [0, 1]`` (несогласие толпы).

    ``min(1, σ / DISP_NORM)``, где ``σ`` — стандартное отклонение прогнозов.
    """
    c = consensus(probabilities)
    variance = math.fsum((p - c) ** 2 for p in probabilities) / len(probabilities)
    return min(1.0, math.sqrt(variance) / DISP_NORM)


def surprise(consensus_probability: float, outcome: int) -> float:
    """Нормированная неожиданность исхода ``û_e ∈ [0, 1]`` (биты под консенсусом).

    ``min(1, −log2(P_крауд(исход)) / SURPRISE_NORM)``, где ``P_крауд`` —
    вероятность фактического исхода по консенсусу толпы. Велика и при расколе
    (≥1 бит), и при провале уверенного консенсуса (много бит).
    """
    p_outcome = consensus_probability if outcome == 1 else 1.0 - consensus_probability
    p_outcome = min(max(p_outcome, _EPS), 1.0)
    return min(1.0, (-math.log2(p_outcome)) / SURPRISE_NORM)


def event_weight(probabilities: Sequence[float], outcome: int) -> float:
    """Вес события по сложности ``w_e ∈ [0, 1]`` (одинаков для всех участников).

    ``β · ĝ_e + (1 − β) · û_e``: смесь разногласия и реализованной
    неожиданности исхода. Тривиальное событие весит ≈0, спорное/неожиданное —
    близко к 1. Нужен для прозрачности («сложность ×N») и порога охвата.
    """
    c = consensus(probabilities)
    return BETA * disagreement(probabilities) + (1.0 - BETA) * surprise(c, outcome)


# ── 2.1/2.4 Превышение над толпой и вклад события ───────────────────────────


def crowd_advantage(
    own: float, probabilities: Sequence[float], outcome: int
) -> float:
    """Превышение над толпой ``Δ = BS(c^{LOO}, o) − BS(p_u, o)``.

    ``Δ > 0`` — игрок точнее leave-one-out консенсуса; ``Δ < 0`` — хуже;
    ``Δ ≈ 0`` — как толпа (следование консенсусу бесприбыльно). Требует ≥ 2
    предсказателей.
    """
    loo = leave_one_out_consensus(probabilities, own)
    return brier(loo, outcome) - brier(own, outcome)


def event_contribution(
    own: float, probabilities: Sequence[float], outcome: int
) -> tuple[float, float]:
    """Вклад игрока за событие: ``(w_e, π = w_e · Δ)``.

    «Взвешенное на сложность превышение над толпой». Возвращает и вес (для
    знаменателя усадки и порога охвата), и сам вклад (для числителя рейтинга).
    """
    weight = event_weight(probabilities, outcome)
    advantage = crowd_advantage(own, probabilities, outcome)
    return weight, weight * advantage


# ── 3.2 Сезонный рейтинг ─────────────────────────────────────────────────────


def season_rating_from_contributions(
    weights: Sequence[float],
    advantages: Sequence[float],
    *,
    k: float = K_SHRINK,
) -> float:
    """Усаженное средневзвешенное превышение над толпой ``R``.

    ``R = Σ(w_e · Δ) / (Σ w_e + k)``. Награждает темп преимущества, а не
    объём: при ``Σw → ∞`` стремится к среднему ``Δ`` на единицу веса, при
    ``Σw → 0`` — к нулю (нет данных — нет рейтинга). Фарм лёгких событий
    нейтрален: ``w≈0, Δ≈0`` добавляют ≈0 и в числитель, и в знаменатель.

    ``weights`` и ``advantages`` — попарно по событиям; ``k`` — цена доверия.
    """
    if len(weights) != len(advantages):
        raise ValueError("Длины weights и advantages должны совпадать")
    numerator = math.fsum(w * d for w, d in zip(weights, advantages, strict=True))
    denominator = math.fsum(weights) + k
    return numerator / denominator


# ── 3.1 Пороги квалификации ─────────────────────────────────────────────────


def qualifies(n_predictions: int, n_categories: int, total_weight: float) -> bool:
    """Прошёл ли пользователь все три порога квалификации к призам.

    Объём (``N_MIN``) ∧ разнообразие категорий (``C_MIN``) ∧ охват сложности
    (``W_MIN``). Порог охвата недостижим фармом тривиальных событий (``w≈0``).
    """
    return (
        n_predictions >= N_MIN
        and n_categories >= C_MIN
        and total_weight >= W_MIN
    )
