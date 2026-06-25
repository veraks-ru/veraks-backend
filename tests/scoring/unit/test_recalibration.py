"""Юнит-тесты межсезонной рекалибровки маппинга «градация → вероятность».

§1.6: правильное значение слова — эмпирический факт; между сезонами номиналы
пересчитываются под фактические частоты с принудительным сохранением
монотонности (изотоническая регрессия, pool-adjacent-violators).
"""

from __future__ import annotations

import pytest

from app.modules.scoring.domain.recalibration import (
    isotonic_increasing,
    recalibrate,
)

APPROX = 1e-6


def test_isotonic_keeps_monotonic_input_unchanged() -> None:
    values = [0.1, 0.3, 0.5, 0.7, 0.9]
    assert isotonic_increasing(values) == pytest.approx(values, abs=APPROX)


def test_isotonic_pools_adjacent_violators() -> None:
    # 0.2 > 0.1 нарушает порядок → объединяются в среднее 0.15.
    result = isotonic_increasing([0.2, 0.1, 0.5])
    assert result == pytest.approx([0.15, 0.15, 0.5], abs=APPROX)
    assert result == sorted(result)  # монотонность восстановлена


def test_isotonic_weighted_pooling() -> None:
    # Веса [1, 3]: (0.8·1 + 0.2·3) / 4 = 0.35.
    result = isotonic_increasing([0.8, 0.2], weights=[1.0, 3.0])
    assert result == pytest.approx([0.35, 0.35], abs=APPROX)


def test_isotonic_preserves_length() -> None:
    assert len(isotonic_increasing([0.9, 0.1, 0.5, 0.3])) == 4


def test_recalibrate_maps_observed_frequencies_when_monotonic() -> None:
    observed = [
        ("definitely_no", 0.05, 100),
        ("probably_no", 0.22, 100),
        ("fifty_fifty", 0.48, 100),
        ("probably_yes", 0.78, 100),  # «Скорее да» сбывается в 78%, не 70%
        ("definitely_yes", 0.93, 100),
    ]
    fitted = recalibrate(observed)
    assert [label for label, _ in fitted] == [label for label, _, _ in observed]
    assert dict(fitted)["probably_yes"] == pytest.approx(0.78, abs=APPROX)


def test_recalibrate_enforces_monotonicity() -> None:
    # Наблюдённая частота «Скорее да» (0.60) ниже «50/50» (0.70) — нарушение.
    observed = [
        ("fifty_fifty", 0.70, 100),
        ("probably_yes", 0.60, 100),
    ]
    fitted = dict(recalibrate(observed))
    assert fitted["fifty_fifty"] <= fitted["probably_yes"]
    assert fitted["fifty_fifty"] == pytest.approx(0.65, abs=APPROX)
    assert fitted["probably_yes"] == pytest.approx(0.65, abs=APPROX)
