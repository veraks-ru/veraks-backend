"""Калибровка прогнозиста — диаграмма надёжности и декомпозиция Brier.

Чистый домен (без I/O). По бинам заявленной вероятности (5 градаций = 5
готовых бакетов) считаем эмпирическую частоту наступления «ДА», доверительный
интервал Уилсона на неё, ECE и полную декомпозицию среднего Brier по Мёрфи:

    BS = Reliability − Resolution + Uncertainty

— калибровка (ниже лучше), различающая способность (выше лучше) и неустранимая
неопределённость событий. Калибровка отдельна от ранга: можно быть идеально
калиброванным и бесполезным (низкий Resolution).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from app.modules.scoring.domain.constants import Z


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    """Один бин диаграммы надёжности (по номиналу градации ``p_g``)."""

    nominal: float
    n: int
    frequency: float  # f_g — эмпирическая частота «ДА» в бине
    ci_low: float  # нижняя граница интервала Уилсона на f_g
    ci_high: float


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    """Полный отчёт калибровки: бины + агрегаты и декомпозиция Мёрфи."""

    bins: tuple[CalibrationBin, ...]
    n_total: int
    ece: float  # Expected Calibration Error — ниже лучше
    reliability: float  # калибровка — ниже лучше
    resolution: float  # различение — выше лучше
    uncertainty: float  # свойство событий (база ДА)
    brier_check: float  # == средний Brier (контроль тождества)


def wilson_interval(p_hat: float, n: int, z: float = Z) -> tuple[float, float]:
    """Доверительный интервал Уилсона на долю ``p_hat`` при ``n`` наблюдениях.

    Корректен на малых выборках (в отличие от нормального приближения). На
    ``n = 0`` возвращает ``(0.0, 1.0)`` — полная неопределённость.
    """
    if n <= 0:
        return (0.0, 1.0)
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p_hat + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p_hat * (1.0 - p_hat) / n + z2 / (4 * n * n))
    return (center - half, center + half)


def calibrate(entries: Sequence[tuple[float, int]]) -> CalibrationReport:
    """Строит отчёт калибровки из пар ``(номинальная вероятность, исход)``.

    Бины — по различным номиналам градаций, отсортированы по возрастанию.
    Пустой вход даёт пустой (нулевой) отчёт.
    """
    n_total = len(entries)
    if n_total == 0:
        return CalibrationReport(
            bins=(),
            n_total=0,
            ece=0.0,
            reliability=0.0,
            resolution=0.0,
            uncertainty=0.0,
            brier_check=0.0,
        )

    # Группировка исходов по номиналу градации.
    grouped: dict[float, list[int]] = {}
    for nominal, outcome in entries:
        grouped.setdefault(nominal, []).append(outcome)

    f_bar = math.fsum(o for _, o in entries) / n_total

    bins: list[CalibrationBin] = []
    ece = reliability = resolution = 0.0
    for nominal in sorted(grouped):
        outcomes = grouped[nominal]
        n_g = len(outcomes)
        f_g = math.fsum(outcomes) / n_g
        low, high = wilson_interval(f_g, n_g)
        bins.append(
            CalibrationBin(
                nominal=nominal,
                n=n_g,
                frequency=f_g,
                ci_low=low,
                ci_high=high,
            )
        )
        weight = n_g / n_total
        ece += weight * abs(f_g - nominal)
        reliability += weight * (nominal - f_g) ** 2
        resolution += weight * (f_g - f_bar) ** 2

    uncertainty = f_bar * (1.0 - f_bar)
    brier_check = reliability - resolution + uncertainty

    return CalibrationReport(
        bins=tuple(bins),
        n_total=n_total,
        ece=ece,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
        brier_check=brier_check,
    )
