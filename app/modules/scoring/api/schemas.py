"""Pydantic-схемы запросов/ответов эндпоинтов scoring.

Контракт HTTP-слоя, отделённый от доменных сущностей: лидерборды и калибровка
отдаются из готовых агрегатов; человеческая формулировка калибровки («когда ты
говоришь X, это сбывается в Y%») собирается на фронте из этих данных.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.scoring.application.dto import GradationRecalibration, ProfileSummary
from app.modules.scoring.domain.calibration import CalibrationReport
from app.modules.scoring.domain.entities import Rating, ScopeType
from app.modules.seasons.domain.value_objects import QualificationResult


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
    # Только для сезонной области; ``None`` для global/category.
    qualified: bool | None = None

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
            qualified=rating.qualified,
        )


class LeaderboardResponse(BaseModel):
    """Страница лидерборда области.

    ``min_resolved`` — применённый порог участия (число разрешённых прогнозов,
    ``LEADERBOARD_MIN_RESOLVED_GLOBAL``/``_CATEGORY``): только для
    global/category при ``qualified_only=true``; ``null`` — если фильтр не
    применялся (``qualified_only=false`` или сезонный лидерборд, у которого
    своя многофакторная квалификация к призам).
    """

    scope_type: ScopeType
    scope_id: uuid.UUID | None
    entries: list[RatingResponse]
    min_resolved: int | None = None


class GradationRecalibrationResponse(BaseModel):
    """Предложение нового номинала градации по итогам прошедшего сезона."""

    nominal: float
    observed_freq: float
    n: int
    fitted: float

    @classmethod
    def from_domain(
        cls, item: GradationRecalibration
    ) -> GradationRecalibrationResponse:
        return cls(
            nominal=item.nominal,
            observed_freq=item.observed_freq,
            n=item.n,
            fitted=item.fitted,
        )


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
    brier_check: float
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
            # Тождество Мёрфи: Brier = reliability − resolution + uncertainty.
            # Отдаём наружу как контроль корректности разложения.
            brier_check=report.brier_check,
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


class ProfileScopeRatingResponse(BaseModel):
    """Метрики пользователя в одной области сводки (без служебных полей области)."""

    rank: int
    skill_score: Decimal
    mean_brier: Decimal
    n_resolved: int

    @classmethod
    def from_domain(cls, rating: Rating) -> ProfileScopeRatingResponse:
        return cls(
            rank=rating.rank,
            skill_score=rating.skill_score,
            mean_brier=rating.mean_brier,
            n_resolved=rating.n_resolved,
        )


class ProfileCategoryRatingResponse(ProfileScopeRatingResponse):
    """Срез сводки по одной категории: название + метрики рейтинга."""

    category_id: uuid.UUID
    slug: str
    title: str


class ProfileSeasonRatingResponse(ProfileScopeRatingResponse):
    """Срез сводки по активному сезону: id сезона + метрики рейтинга."""

    season_id: uuid.UUID


class ProfileSummaryResponse(BaseModel):
    """Сводка публичного профиля: global / по категориям / активный сезон.

    ``global_`` сериализуется в JSON как ``"global"`` (зарезервированное слово
    Python нельзя использовать именем поля модели).
    """

    model_config = ConfigDict(populate_by_name=True)

    user_id: uuid.UUID
    global_: ProfileScopeRatingResponse | None = Field(
        default=None, alias="global"
    )
    categories: list[ProfileCategoryRatingResponse]
    season: ProfileSeasonRatingResponse | None = None

    @classmethod
    def from_domain(cls, summary: ProfileSummary) -> ProfileSummaryResponse:
        """Маппинг доменной сводки в ответ; ``None``/``[]`` — легальные значения."""
        season = None
        if summary.season_rating is not None and summary.active_season_id is not None:
            season = ProfileSeasonRatingResponse(
                season_id=summary.active_season_id,
                rank=summary.season_rating.rank,
                skill_score=summary.season_rating.skill_score,
                mean_brier=summary.season_rating.mean_brier,
                n_resolved=summary.season_rating.n_resolved,
            )
        return cls(
            user_id=summary.user_id,
            global_=(
                ProfileScopeRatingResponse.from_domain(summary.global_rating)
                if summary.global_rating is not None
                else None
            ),
            categories=[
                ProfileCategoryRatingResponse(
                    category_id=item.category.category_id,
                    slug=item.category.slug,
                    title=item.category.title,
                    rank=item.rating.rank,
                    skill_score=item.rating.skill_score,
                    mean_brier=item.rating.mean_brier,
                    n_resolved=item.rating.n_resolved,
                )
                for item in summary.categories
            ],
            season=season,
        )


class QualificationResponse(BaseModel):
    """Разбор квалификации пользователя в сезоне (почему да/нет)."""

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

    @classmethod
    def from_domain(cls, result: QualificationResult) -> QualificationResponse:
        """Маппинг доменного результата квалификации в ответ."""
        return cls(
            qualified=result.qualified,
            volume_ok=result.volume_ok,
            diversity_ok=result.diversity_ok,
            coverage_ok=result.coverage_ok,
            n_resolved=result.n_resolved,
            category_count=result.category_count,
            total_weight=result.total_weight,
            n_min=result.n_min,
            c_min=result.c_min,
            w_min=result.w_min,
        )


class ScoreEventResponse(BaseModel):
    """Результат запуска скоринга события."""

    event_id: uuid.UUID
    scored: int


class RecomputeRatingsResponse(BaseModel):
    """Результат полного пересчёта рейтингов."""

    upserted: int


class FinalizeSeasonResponse(BaseModel):
    """Результат финализации сезона (ручной admin-триггер).

    ``finalized=False`` — идемпотентный no-op (сезон уже был завершён).
    """

    season_id: uuid.UUID
    finalized: bool
    qualified_count: int
    total_participants: int
