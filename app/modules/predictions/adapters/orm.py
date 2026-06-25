"""ORM-модель predictions (SQLAlchemy 2.0).

Маппится на доменную сущность в обе стороны через явные ``to_domain`` /
``from_domain``, чтобы домен оставался свободным от инфраструктуры.
``probability`` хранится как ``numeric(3,2)`` (Decimal), ``brier_score`` —
``numeric(6,5)``; денежно-точные типы, без float.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, Numeric
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.predictions.domain.entities import ConfidenceGrade, Prediction

_grade_enum = SAEnum(
    ConfidenceGrade,
    name="confidence_grade",
    values_callable=lambda enum: [member.value for member in enum],
)


class PredictionORM(Base):
    """Таблица ``predictions`` — прогноз на пользователя на событие.

    Ядро антифрода/честности — ``UNIQUE(user_id, event_id)``: один прогноз на
    пару. Хранится latest-wins; история изменений — в ``audit_log``.
    """

    __tablename__ = "predictions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    confidence_grade: Mapped[ConfidenceGrade] = mapped_column(
        _grade_enum, nullable=False
    )
    probability: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False)
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    brier_score: Mapped[Decimal | None] = mapped_column(Numeric(6, 5), nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Prediction:
        """ORM → доменная сущность."""
        return Prediction(
            id=self.id,
            user_id=self.user_id,
            event_id=self.event_id,
            confidence_grade=self.confidence_grade,
            probability=self.probability,
            is_locked=self.is_locked,
            brier_score=self.brier_score,
            scored_at=self.scored_at,
            created_at=self.created_at,
            updated_at=self.updated_at,
        )

    @classmethod
    def from_domain(cls, prediction: Prediction) -> PredictionORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=prediction.id,
            user_id=prediction.user_id,
            event_id=prediction.event_id,
            confidence_grade=prediction.confidence_grade,
            probability=prediction.probability,
            is_locked=prediction.is_locked,
            brier_score=prediction.brier_score,
            scored_at=prediction.scored_at,
            created_at=prediction.created_at,
            updated_at=prediction.updated_at,
        )
