"""Pydantic-схемы запросов/ответов эндпоинтов scoring.

Контракт HTTP-слоя, отделённый от доменных сущностей: лидерборды и калибровка
отдаются из готовых агрегатов; человеческая формулировка калибровки («когда ты
говоришь X, это сбывается в Y%») собирается на фронте из этих данных.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.modules.scoring.domain.calibration import CalibrationReport
from app.modules.scoring.domain.entities import Rating, ScopeType


class RatingResponse(BaseModel):
    """Строка лидерборда / профиля: предрасчитанные метрики области."""

    model_config = ConfigDict(from_attributes=True)

    user_id: uuid.UUID
    scope_type: ScopeType
    scope_id: uuid.UUID | None
    mean_brier: Decimal
    skill_score: Decimal
    calibration_error: Decimal
    n_resolved: int
    rank: int

    @classmethod
    def from_domain(cls, rating: Rating) -> RatingResponse:
        """Маппинг доменной сущности рейтинга в ответ."""
        return cls(
            user_id=rating.user_id,
            scope_type=rating.scope_type,
            scope_id=rating.scope_id,
            mean_brier=rating.mean_brier,
            skill_score=rating.skill_score,
            calibration_error=rating.calibration_error,
            n_resolved=rating.n_resolved,
            rank=rating.rank,
        )


class LeaderboardResponse(BaseModel):
    """Страница лидерборда области."""

    scope_type: ScopeType
    scope_id: uuid.UUID | None
    entries: list[RatingResponse]


class CalibrationBinResponse(BaseModel):
    """Один бин диаграммы надёжности (номинал vs факт + интервал Уилсона)."""

    nominal: float
    n: int
    frequency: float
    ci_low: float
    ci_high: float


class CalibrationResponse(BaseModel):
    """Калибровка профиля: бины + декомпозиция Brier по Мёрфи."""

    user_id: uuid.UUID
    n_total: int
    ece: float
    reliability: float
    resolution: float
    uncertainty: float
    bins: list[CalibrationBinResponse]

    @classmethod
    def from_report(
        cls, user_id: uuid.UUID, report: CalibrationReport
    ) -> CalibrationResponse:
        """Маппинг доменного отчёта калибровки в ответ."""
        return cls(
            user_id=user_id,
            n_total=report.n_total,
            ece=report.ece,
            reliability=report.reliability,
            resolution=report.resolution,
            uncertainty=report.uncertainty,
            bins=[
                CalibrationBinResponse(
                    nominal=b.nominal,
                    n=b.n,
                    frequency=b.frequency,
                    ci_low=b.ci_low,
                    ci_high=b.ci_high,
                )
                for b in report.bins
            ],
        )


class ScoreEventResponse(BaseModel):
    """Результат запуска скоринга события."""

    event_id: uuid.UUID
    scored: int


class RecomputeRatingsResponse(BaseModel):
    """Результат полного пересчёта рейтингов."""

    upserted: int
