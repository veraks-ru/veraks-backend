"""Межсезонная рекалибровка маппинга «градация → вероятность» (чистый домен).

Старт сезона фиксирует номиналы ``0.1/0.3/0.5/0.7/0.9``; к следующему сезону
они пересчитываются под фактические частоты наступления «ДА» в каждой
градации. Изотоническая регрессия (pool-adjacent-violators) принудительно
сохраняет порядок ``Точно нет < … < Точно да``, чтобы слова не «перепутались».

Менять маппинг внутри сезона нельзя (условия публичного конкурса фиксируются
заранее) — поэтому это отдельный оффлайн-расчёт, а не часть онлайн-скоринга.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass
class _Block:
    """Блок объединённых значений в алгоритме PAV (взвешенное среднее)."""

    total: float  # Σ value·weight
    weight: float  # Σ weight
    count: int

    @property
    def value(self) -> float:
        return self.total / self.weight


def isotonic_increasing(
    values: Sequence[float], weights: Sequence[float] | None = None
) -> list[float]:
    """Изотоническая регрессия: ближайшая монотонно неубывающая последовательность.

    Алгоритм pool-adjacent-violators (взвешенный). При нарушении порядка
    соседние значения объединяются в общий взвешенный средний уровень.
    Возвращает значения той же длины, что и вход.
    """
    if weights is None:
        weights = [1.0] * len(values)
    if len(values) != len(weights):
        raise ValueError("Длины values и weights должны совпадать")

    blocks: list[_Block] = []
    for value, weight in zip(values, weights, strict=True):
        block = _Block(total=value * weight, weight=weight, count=1)
        # Сливаем с предыдущим, пока он нарушает монотонность (его уровень выше).
        while blocks and blocks[-1].value > block.value:
            prev = blocks.pop()
            block = _Block(
                total=prev.total + block.total,
                weight=prev.weight + block.weight,
                count=prev.count + block.count,
            )
        blocks.append(block)

    result: list[float] = []
    for block in blocks:
        result.extend([block.value] * block.count)
    return result


def recalibrate(
    observed: Sequence[tuple[str, float, int]],
) -> list[tuple[str, float]]:
    """Пересчитывает номиналы градаций по наблюдённым частотам прошлого сезона.

    ``observed`` — упорядоченный (от «Точно нет» к «Точно да») список
    ``(метка_градации, наблюдённая_частота_ДА, объём_выборки)``. Возвращает
    ``[(метка, новый_номинал)]`` в том же порядке, с гарантией монотонности
    (взвешенной по объёму выборки изотонией).
    """
    labels = [label for label, _, _ in observed]
    freqs = [freq for _, freq, _ in observed]
    weights = [float(max(n, 1)) for _, _, n in observed]
    fitted = isotonic_increasing(freqs, weights)
    return list(zip(labels, fitted, strict=True))
