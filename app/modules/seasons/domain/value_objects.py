"""Value-objects домена seasons.

``LeagueConfig`` — **замороженный снапшот правил лиги** на сезон: маппинг
градаций в вероятности и пороги квалификации. Снимок делается при активации
сезона и больше не меняется (правила публикуются заранее, ретро-пересчёт
запрещён — см. спецификацию скоринга §1.6). Чистый код без I/O; в БД хранится
как ``jsonb`` через ``to_dict``/``from_dict``.

Важно (ацикличность зависимостей): значения по умолчанию (`default`) заданы
литералами и **не импортируют** домен scoring. Снапшот «боевых» дефолтов
scoring передаётся в use-case активации извне (composition root), а этот
fallback нужен лишь как самодостаточная нейтральная сетка.

``QualificationResult`` — итог проверки порогов с побитовым разбором «почему
не квалифицирован» (для UX профиля).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.modules.seasons.domain.errors import InvalidSeasonDataError


@dataclass(frozen=True, slots=True)
class LeagueConfig:
    """Снапшот правил сезона: сетка градаций + пороги квалификации и скоринга.

    * ``gradation_map`` — строго возрастающий кортеж вероятностей в ``(0, 1)``;
    * ``n_min`` / ``c_min`` / ``w_min`` — пороги квалификации (объём /
      разнообразие категорий / охват сложности);
    * ``m_per_category`` — минимум прогнозов в категории, чтобы она
      засчитывалась в разнообразие;
    * ``k_shrink`` — константа усадки сезонного рейтинга;
    * ``min_predictors`` — минимум предсказателей, чтобы событие считалось
      «рейтинговым» (LOO-консенсусу нужно ≥ 2 голоса).
    """

    gradation_map: tuple[float, ...]
    n_min: int
    c_min: int
    w_min: float
    m_per_category: int
    k_shrink: float
    min_predictors: int

    def __post_init__(self) -> None:
        grid = self.gradation_map
        if len(grid) < 2:
            raise InvalidSeasonDataError("Сетка градаций должна содержать ≥ 2 точек")
        if any(not (0.0 < p < 1.0) for p in grid):
            raise InvalidSeasonDataError(
                "Все градации должны лежать строго в интервале (0, 1)"
            )
        if any(a >= b for a, b in zip(grid, grid[1:], strict=False)):
            raise InvalidSeasonDataError("Сетка градаций должна строго возрастать")
        if self.n_min < 0:
            raise InvalidSeasonDataError("n_min не может быть отрицательным")
        if self.c_min < 1:
            raise InvalidSeasonDataError("c_min должен быть ≥ 1")
        if self.w_min < 0:
            raise InvalidSeasonDataError("w_min не может быть отрицательным")
        if self.m_per_category < 1:
            raise InvalidSeasonDataError("m_per_category должен быть ≥ 1")
        if self.k_shrink <= 0:
            raise InvalidSeasonDataError("k_shrink должен быть положительным")
        if self.min_predictors < 2:
            raise InvalidSeasonDataError("min_predictors должен быть ≥ 2 (LOO)")

    @classmethod
    def default(cls) -> LeagueConfig:
        """Нейтральная самодостаточная конфигурация (fallback, без импорта scoring)."""
        return cls(
            gradation_map=(0.1, 0.3, 0.5, 0.7, 0.9),
            n_min=30,
            c_min=4,
            w_min=8.0,
            m_per_category=1,
            k_shrink=6.0,
            min_predictors=5,
        )

    def to_dict(self) -> dict[str, Any]:
        """Сериализация в ``jsonb``-совместимый словарь."""
        return {
            "gradation_map": list(self.gradation_map),
            "n_min": self.n_min,
            "c_min": self.c_min,
            "w_min": self.w_min,
            "m_per_category": self.m_per_category,
            "k_shrink": self.k_shrink,
            "min_predictors": self.min_predictors,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LeagueConfig:
        """Десериализация из ``jsonb`` (с валидацией в ``__post_init__``)."""
        try:
            return cls(
                gradation_map=tuple(float(p) for p in data["gradation_map"]),
                n_min=int(data["n_min"]),
                c_min=int(data["c_min"]),
                w_min=float(data["w_min"]),
                m_per_category=int(data["m_per_category"]),
                k_shrink=float(data["k_shrink"]),
                min_predictors=int(data["min_predictors"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidSeasonDataError(
                f"Некорректный снапшот league_config: {exc}"
            ) from exc


@dataclass(frozen=True, slots=True)
class QualificationResult:
    """Итог проверки квалификации к призам с разбором по порогам."""

    qualified: bool
    volume_ok: bool
    diversity_ok: bool
    coverage_ok: bool
    n_resolved: int
    category_count: int
    total_weight: float
    n_min: int
    c_min: int
    w_min: float


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class SeasonFinalizationEntry:
    """Строка неизменяемого снапшота финализации — один квалифицированный участник.

    Хранится строкой-на-участника (не одним jsonb-блобом), чтобы снапшот сезона
    с десятками тысяч участников не упирался в размер одной ячейки (дизайн §6.3).
    """

    user_id: uuid.UUID
    rank: int
    skill_score: Decimal
    mean_brier: Decimal
    calibration_error: Decimal
    n_resolved: int


@dataclass(frozen=True, slots=True)
class SeasonFinalization:
    """Неизменяемая запись о финализации сезона (момент определения призёров).

    Фиксирует, какой ``LeagueConfig`` применён и когда; ранжированный снапшот
    участников лежит в дочерних :class:`SeasonFinalizationEntry`. Append-only:
    у роли приложения нет ``UPDATE``/``DELETE`` (как у ``resolutions``).
    """

    season_id: uuid.UUID
    league_config: LeagueConfig
    qualified_count: int
    total_participants: int
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    finalized_at: datetime = field(default_factory=_utcnow)
