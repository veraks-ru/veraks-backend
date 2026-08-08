"""Юнит-тесты чистой логики повышения/понижения между дивизионами."""

from __future__ import annotations

import uuid

from app.modules.leagues.domain.promotion import (
    compute_initial_placement,
    compute_promotion,
)


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


# ── Первичный посев (сезон без предшественника) ──────────────────────────────


def test_initial_placement_cold_start_puts_everyone_in_lowest() -> None:
    """Холодный старт: высший дивизион никем не заслужен."""
    users = [uuid.uuid4() for _ in range(5)]

    result = compute_initial_placement(users, num_levels=3, even_split=False)

    assert set(result.values()) == {3}
    assert len(result) == 5


def test_initial_placement_even_split_fills_top_first() -> None:
    """7 человек на 3 уровня → 3/2/2: остаток достаётся верхним дивизионам."""
    users = [uuid.uuid4() for _ in range(7)]

    result = compute_initial_placement(users, num_levels=3, even_split=True)

    by_level: dict[int, list[uuid.UUID]] = {}
    for user_id, level in result.items():
        by_level.setdefault(level, []).append(user_id)
    assert sorted(len(v) for v in by_level.values()) == [2, 2, 3]
    # Порядок сохранён: сильнейшие — в первом дивизионе.
    assert [result[u] for u in users] == [1, 1, 1, 2, 2, 3, 3]


def test_initial_placement_more_levels_than_people() -> None:
    a, b = uuid.uuid4(), uuid.uuid4()

    result = compute_initial_placement([a, b], num_levels=5, even_split=True)

    # Никто не теряется, все уровни валидны.
    assert set(result) == {a, b}
    assert all(1 <= lvl <= 5 for lvl in result.values())


def test_initial_placement_empty_input() -> None:
    assert compute_initial_placement([], num_levels=3, even_split=True) == {}
