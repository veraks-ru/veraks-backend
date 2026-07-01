"""Юнит-тесты чистой логики повышения/понижения между дивизионами."""

from __future__ import annotations

import uuid

from app.modules.leagues.domain.promotion import compute_promotion


def test_promotion_moves_top_up_and_bottom_down() -> None:
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()  # дивизион 1
    d, e, f = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()  # дивизион 2
    g, h = uuid.uuid4(), uuid.uuid4()  # дивизион 3
    standings = {1: [a, b, c], 2: [d, e, f], 3: [g, h]}

    result = compute_promotion(standings, num_levels=3, promote=1, relegate=1)

    # Высший дивизион не повышает; худший из него падает во второй.
    assert result[a] == 1
    assert result[b] == 1
    assert result[c] == 2
    # Средний: топ поднимается, низ падает, середина остаётся.
    assert result[d] == 1
    assert result[e] == 2
    assert result[f] == 3
    # Низший не понижает; топ поднимается.
    assert result[g] == 2
    assert result[h] == 3


def test_promotion_single_top_division_is_stable() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()
    result = compute_promotion({1: [a, b]}, num_levels=1, promote=1, relegate=1)
    # Единственный дивизион — все остаются на уровне 1.
    assert result == {a: 1, b: 1}


def test_promotion_empty_division_ok() -> None:
    result = compute_promotion({1: [], 2: []}, num_levels=2)
    assert result == {}
