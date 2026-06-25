"""Юнит-тесты калибровки профиля (``scoring.domain.calibration``).

Проверяют диаграмму надёжности по бинам уверенности, ECE, доверительный
интервал Уилсона и декомпозицию Brier по Мёрфи (тождество
``Reliability − Resolution + Uncertainty == средний Brier``).
"""

from __future__ import annotations

import pytest

from app.modules.scoring.domain.calibration import calibrate, wilson_interval
from app.modules.scoring.domain.formulas import brier

APPROX = 1e-4


def test_wilson_interval_for_spec_example() -> None:
    # §4.4: f=0.775, n=40, z=1.96 → CI ≈ [0.622, 0.879].
    low, high = wilson_interval(0.775, 40)
    assert low == pytest.approx(0.625, abs=0.01)
    assert high == pytest.approx(0.877, abs=0.01)
    assert low < 0.775 < high


def test_wilson_interval_widens_for_small_samples() -> None:
    wide = wilson_interval(0.5, 4)
    narrow = wilson_interval(0.5, 400)
    assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])


def test_calibration_single_bin_spec_example() -> None:
    """§4.4: «Скорее да» (0.70), 40 прогнозов, 31 сбылся → f_g=0.775."""
    entries = [(0.70, 1)] * 31 + [(0.70, 0)] * 9
    report = calibrate(entries)

    assert report.n_total == 40
    assert len(report.bins) == 1
    bin_ = report.bins[0]
    assert bin_.nominal == pytest.approx(0.70)
    assert bin_.n == 40
    assert bin_.frequency == pytest.approx(0.775, abs=APPROX)

    assert report.ece == pytest.approx(0.075, abs=APPROX)
    assert report.reliability == pytest.approx(0.005625, abs=APPROX)
    assert report.resolution == pytest.approx(0.0, abs=APPROX)  # один бин
    assert report.uncertainty == pytest.approx(0.174375, abs=APPROX)


def test_murphy_decomposition_identity_holds() -> None:
    """Reliability − Resolution + Uncertainty == средний Brier (тождество)."""
    entries = (
        [(0.30, 1)] * 2
        + [(0.30, 0)] * 8
        + [(0.90, 1)] * 8
        + [(0.90, 0)] * 2
        + [(0.50, 1)] * 5
        + [(0.50, 0)] * 5
    )
    report = calibrate(entries)

    mean_brier = sum(brier(p, o) for p, o in entries) / len(entries)
    assert report.brier_check == pytest.approx(mean_brier, abs=APPROX)
    assert (
        report.reliability - report.resolution + report.uncertainty
        == pytest.approx(mean_brier, abs=APPROX)
    )
    assert report.resolution > 0.0  # бины различают исходы


def test_calibration_bins_sorted_ascending() -> None:
    entries = [(0.90, 1), (0.10, 0), (0.50, 1), (0.30, 0)]
    report = calibrate(entries)
    nominals = [b.nominal for b in report.bins]
    assert nominals == sorted(nominals)


def test_calibration_empty_is_empty_report() -> None:
    report = calibrate([])
    assert report.n_total == 0
    assert report.bins == ()
    assert report.ece == pytest.approx(0.0)
