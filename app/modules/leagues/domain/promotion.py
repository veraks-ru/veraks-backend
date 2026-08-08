"""Чистая логика повышения/понижения между дивизионами.

Вход — стендинги завершённого сезона по уровням (``level → [user_id]`` от
лучшего к худшему). Выход — уровень каждого пользователя на следующий сезон.
Правила: топ-``promote`` каждого дивизиона поднимаются на уровень выше (кроме
высшего), низ-``relegate`` опускаются ниже (кроме низшего), остальные остаются.
Без I/O — легко юнит-тестируется.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence


def compute_initial_placement(
    ranked: Sequence[uuid.UUID],
    *,
    num_levels: int,
    even_split: bool,
) -> dict[uuid.UUID, int]:
    """Первичная раскладка по дивизионам для сезона без предшественника.

    ``ranked`` — участники от сильнейшего к слабейшему (без трек-рекорда порядок
    произволен, и это нормально: раскладка всё равно временная до первой
    промоции). ``even_split=False`` — все в низший дивизион (честный холодный
    старт: высший уровень никем не заслужен). ``even_split=True`` — поровну по
    уровням сверху вниз; остаток от деления достаётся верхним дивизионам.

    Без I/O — как и :func:`compute_promotion`.
    """
    if num_levels < 1:
        return {}
    members = list(ranked)
    if not members:
        return {}
    if not even_split:
        return {user_id: num_levels for user_id in members}

    n = len(members)
    base, extra = divmod(n, num_levels)
    result: dict[uuid.UUID, int] = {}
    cursor = 0
    for level in range(1, num_levels + 1):
        # Остаток раздаём верхним уровням — они заполняются первыми.
        size = base + (1 if level <= extra else 0)
        for user_id in members[cursor : cursor + size]:
            result[user_id] = level
        cursor += size
    # Хвост (возможен только при num_levels > n) — в низший.
    for user_id in members[cursor:]:
        result[user_id] = num_levels
    return result


def compute_promotion(
    standings_by_level: Mapping[int, Sequence[uuid.UUID]],
    *,
    num_levels: int,
    promote: int = 2,
    relegate: int = 2,
) -> dict[uuid.UUID, int]:
    """Считает уровень каждого пользователя на следующий сезон.

    ``num_levels`` — число дивизионов (уровни ``1..num_levels``). Высший (1) не
    повышает, низший (``num_levels``) не понижает. Пустые/короткие дивизионы
    обрабатываются корректно (перекрытия зон промо/релегации не двигают дважды).
    """
    result: dict[uuid.UUID, int] = {}
    for level, ranked in standings_by_level.items():
        members = list(ranked)
        n = len(members)
        top = set(range(min(promote, n)))
        bottom = set(range(max(0, n - relegate), n))
        for i, user_id in enumerate(members):
            new_level = level
            if i in top and level > 1:
                new_level = level - 1
            elif i in bottom and level < num_levels:
                new_level = level + 1
            result[user_id] = new_level
    return result
