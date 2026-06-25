"""Pydantic-схемы запросов/ответов эндпоинтов seasons."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import LeagueConfig


class LeagueConfigSchema(BaseModel):
    """Снапшот правил лиги (маппинг градаций + пороги квалификации/скоринга)."""

    gradation_map: list[float]
    n_min: int
    c_min: int
    w_min: float
    m_per_category: int
    k_shrink: float
    min_predictors: int

    @classmethod
    def from_domain(cls, config: LeagueConfig) -> LeagueConfigSchema:
        return cls(
            gradation_map=list(config.gradation_map),
            n_min=config.n_min,
            c_min=config.c_min,
            w_min=config.w_min,
            m_per_category=config.m_per_category,
            k_shrink=config.k_shrink,
            min_predictors=config.min_predictors,
        )

    def to_domain(self) -> LeagueConfig:
        """В доменный VO (валидация инвариантов — в ``LeagueConfig.__post_init__``)."""
        return LeagueConfig(
            gradation_map=tuple(self.gradation_map),
            n_min=self.n_min,
            c_min=self.c_min,
            w_min=self.w_min,
            m_per_category=self.m_per_category,
            k_shrink=self.k_shrink,
            min_predictors=self.min_predictors,
        )


class SeasonResponse(BaseModel):
    """Представление сезона для API."""

    id: uuid.UUID
    slug: str
    title: str
    starts_at: datetime
    ends_at: datetime
    status: SeasonStatus
    league_config: LeagueConfigSchema | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, season: Season) -> SeasonResponse:
        return cls(
            id=season.id,
            slug=season.slug,
            title=season.title,
            starts_at=season.starts_at,
            ends_at=season.ends_at,
            status=season.status,
            league_config=(
                LeagueConfigSchema.from_domain(season.league_config)
                if season.league_config is not None
                else None
            ),
            created_at=season.created_at,
            updated_at=season.updated_at,
        )


class SeasonListResponse(BaseModel):
    """Список сезонов."""

    items: list[SeasonResponse]


class CreateSeasonRequest(BaseModel):
    """Тело создания сезона (editor/admin)."""

    slug: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1)
    starts_at: datetime
    ends_at: datetime


class UpdateSeasonRequest(BaseModel):
    """Тело правки сезона (только пока ``upcoming``); все поля опциональны."""

    slug: str | None = Field(default=None, min_length=1, max_length=64)
    title: str | None = Field(default=None, min_length=1)
    starts_at: datetime | None = None
    ends_at: datetime | None = None


class ActivateSeasonRequest(BaseModel):
    """Тело активации: опциональный кастомный снапшот правил лиги.

    Если ``league_config`` не передан — берётся нейтральный дефолт seasons
    (``LeagueConfig.default()``). Снапшот фиксируется и далее не меняется.
    """

    league_config: LeagueConfigSchema | None = None
