"""Чистая политика квалификации к призам (объём / разнообразие / охват).

Все три порога обязательны (см. спецификацию скоринга §3.1). Функция
детерминирована и не делает I/O — входные агрегаты (число засчитанных
прогнозов, число категорий, суммарный вес сложности) считает вызывающий слой
(``RecomputeRatings`` в scoring), а пороги берёт из замороженного
``LeagueConfig`` сезона.

``category_count`` — число категорий, в каждой из которых у пользователя не
меньше ``cfg.m_per_category`` засчитанных прогнозов (фильтрация — на стороне
накопителя, здесь принимается уже готовое число).
"""

from __future__ import annotations

from app.modules.seasons.domain.value_objects import LeagueConfig, QualificationResult


def evaluate_qualification(
    *,
    n_resolved: int,
    category_count: int,
    total_weight: float,
    cfg: LeagueConfig,
) -> QualificationResult:
    """Проверяет три порога квалификации и возвращает разбор результата."""
    volume_ok = n_resolved >= cfg.n_min
    diversity_ok = category_count >= cfg.c_min
    coverage_ok = total_weight >= cfg.w_min
    return QualificationResult(
        qualified=volume_ok and diversity_ok and coverage_ok,
        volume_ok=volume_ok,
        diversity_ok=diversity_ok,
        coverage_ok=coverage_ok,
        n_resolved=n_resolved,
        category_count=category_count,
        total_weight=total_weight,
        n_min=cfg.n_min,
        c_min=cfg.c_min,
        w_min=cfg.w_min,
    )
