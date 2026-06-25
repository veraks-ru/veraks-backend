"""ORM-модели seasons (SQLAlchemy 2.0).

Маппятся на доменные сущности в обе стороны через явные ``to_domain`` /
``from_domain``. ``league_config`` хранится как ``jsonb`` (NULL до активации);
снапшот финализации разложен на родителя ``season_finalizations`` и строки-на-
участника ``season_finalization_entries`` — append-only (дизайн §6.3).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Enum as SAEnum, ForeignKey, Index, Integer, Numeric, Text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.seasons.domain.entities import Season, SeasonStatus
from app.modules.seasons.domain.value_objects import (
    LeagueConfig,
    SeasonFinalization,
    SeasonFinalizationEntry,
)

_status_enum = SAEnum(
    SeasonStatus,
    name="season_status",
    values_callable=lambda enum: [member.value for member in enum],
)


class SeasonORM(Base):
    """Таблица ``seasons`` — соревновательные периоды с замороженными правилами."""

    __tablename__ = "seasons"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(CITEXT, unique=True, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    ends_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    status: Mapped[SeasonStatus] = mapped_column(
        _status_enum, nullable=False, index=True
    )
    # Снапшот LeagueConfig; NULL пока сезон не активирован.
    league_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Season:
        """ORM → доменная сущность (восстановление ``LeagueConfig`` из jsonb)."""
        return Season(
            id=self.id,
            slug=self.slug,
            title=self.title,
            starts_at=self.starts_at,
            ends_at=self.ends_at,
            status=self.status,
            league_config=(
                LeagueConfig.from_dict(self.league_config)
                if self.league_config is not None
                else None
            ),
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, season: Season) -> SeasonORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=season.id,
            slug=season.slug,
            title=season.title,
            starts_at=season.starts_at,
            ends_at=season.ends_at,
            status=season.status,
            league_config=(
                season.league_config.to_dict()
                if season.league_config is not None
                else None
            ),
            created_at=season.created_at,
            updated_at=season.updated_at,
        )


class SeasonFinalizationORM(Base):
    """Таблица ``season_finalizations`` — неизменяемые записи финализаций."""

    __tablename__ = "season_finalizations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    season_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("seasons.id"), nullable=False, index=True
    )
    finalized_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    league_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    qualified_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_participants: Mapped[int] = mapped_column(Integer, nullable=False)

    def to_domain(self) -> SeasonFinalization:
        """ORM → доменный value-object."""
        return SeasonFinalization(
            id=self.id,
            season_id=self.season_id,
            league_config=LeagueConfig.from_dict(self.league_config),
            qualified_count=self.qualified_count,
            total_participants=self.total_participants,
            finalized_at=self.finalized_at,
        )

    @classmethod
    def from_domain(cls, finalization: SeasonFinalization) -> SeasonFinalizationORM:
        """Доменный value-object → новая ORM-строка."""
        return cls(
            id=finalization.id,
            season_id=finalization.season_id,
            finalized_at=finalization.finalized_at,
            league_config=finalization.league_config.to_dict(),
            qualified_count=finalization.qualified_count,
            total_participants=finalization.total_participants,
        )


class SeasonFinalizationEntryORM(Base):
    """Таблица ``season_finalization_entries`` — строка-на-участника снапшота."""

    __tablename__ = "season_finalization_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    finalization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("season_finalizations.id"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    rank: Mapped[int] = mapped_column(Integer, nullable=False)
    skill_score: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    mean_brier: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    calibration_error: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    n_resolved: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index(
            "ix_season_finalization_entries_finalization_rank",
            "finalization_id",
            "rank",
        ),
    )

    @classmethod
    def from_domain(
        cls, finalization_id: uuid.UUID, entry: SeasonFinalizationEntry
    ) -> SeasonFinalizationEntryORM:
        """Доменный value-object → новая ORM-строка (привязка к финализации)."""
        return cls(
            id=uuid.uuid4(),
            finalization_id=finalization_id,
            user_id=entry.user_id,
            rank=entry.rank,
            skill_score=entry.skill_score,
            mean_brier=entry.mean_brier,
            calibration_error=entry.calibration_error,
            n_resolved=entry.n_resolved,
        )
