"""ORM-модель ``ratings`` (SQLAlchemy 2.0).

Материализованный агрегат лидербордов/профилей. Маппится на доменную сущность
:class:`Rating` через явные ``to_domain``/``from_domain``. Метрики — ``numeric(6,5)``
(Decimal, без float).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Enum as SAEnum, ForeignKey, Index, Integer, Numeric
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.scoring.domain.entities import Rating, ScopeType

_scope_enum = SAEnum(
    ScopeType,
    name="rating_scope",
    values_callable=lambda enum: [member.value for member in enum],
)


class RatingORM(Base):
    """Таблица ``ratings`` — предрасчитанные агрегаты точности по областям.

    Уникальность ``(user_id, scope_type, scope_id)`` (с учётом ``NULL`` для
    global) обеспечивается выражённым unique-индексом в миграции. Горячее
    чтение топа идёт по ``(scope_type, scope_id, rank)``.
    """

    __tablename__ = "ratings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    scope_type: Mapped[ScopeType] = mapped_column(_scope_enum, nullable=False)
    scope_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    mean_brier: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    skill_score: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    calibration_error: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    n_resolved: Mapped[int] = mapped_column(Integer, nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    __table_args__ = (
        # Горячее чтение топа области.
        Index("ix_ratings_scope_rank", "scope_type", "scope_id", "rank"),
        # Чтение по среднему Brier (альтернативная сортировка/аналитика).
        Index("ix_ratings_scope_mean_brier", "scope_type", "scope_id", "mean_brier"),
    )

    def to_domain(self) -> Rating:
        """ORM → доменная сущность."""
        return Rating(
            id=self.id,
            user_id=self.user_id,
            scope_type=self.scope_type,
            scope_id=self.scope_id,
            mean_brier=self.mean_brier,
            skill_score=self.skill_score,
            calibration_error=self.calibration_error,
            n_resolved=self.n_resolved,
            rank=self.rank,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, rating: Rating) -> RatingORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=rating.id,
            user_id=rating.user_id,
            scope_type=rating.scope_type,
            scope_id=rating.scope_id,
            mean_brier=rating.mean_brier,
            skill_score=rating.skill_score,
            calibration_error=rating.calibration_error,
            n_resolved=rating.n_resolved,
            rank=rating.rank,
            updated_at=rating.updated_at,
        )
