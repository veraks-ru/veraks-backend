"""ORM-модели resolutions (SQLAlchemy 2.0).

Маппятся на доменные сущности через явные ``to_domain``/``from_domain``.
Неизменяемость ``resolutions`` гарантируется схемным триггером (миграция
``0009``), а не ORM. ``resolution_scoring_dispatches`` — служебная таблица
маркеров скоринга (без доменной сущности).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, Enum as SAEnum, ForeignKey, Text
from sqlalchemy.dialects.postgresql import TIMESTAMP, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.modules.resolutions.domain.entities import (
    Dispute,
    DisputeStatus,
    Resolution,
    ResolutionStatus,
)

_resolution_status_enum = SAEnum(
    ResolutionStatus,
    name="resolution_status",
    values_callable=lambda enum: [member.value for member in enum],
)
_dispute_status_enum = SAEnum(
    DisputeStatus,
    name="dispute_status",
    values_callable=lambda enum: [member.value for member in enum],
)


class ResolutionORM(Base):
    """Таблица ``resolutions`` — append-only журнал решений по событиям."""

    __tablename__ = "resolutions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    outcome: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[ResolutionStatus] = mapped_column(
        _resolution_status_enum, nullable=False
    )
    resolved_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    source_reference: Mapped[str] = mapped_column(Text, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resolutions.id"), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolved_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    def to_domain(self) -> Resolution:
        """ORM → доменная сущность."""
        return Resolution(
            id=self.id,
            event_id=self.event_id,
            outcome=self.outcome,
            status=self.status,
            resolved_by=self.resolved_by,
            source_reference=self.source_reference,
            supersedes_id=self.supersedes_id,
            notes=self.notes,
            resolved_at=self.resolved_at,
        )

    @classmethod
    def from_domain(cls, resolution: Resolution) -> ResolutionORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=resolution.id,
            event_id=resolution.event_id,
            outcome=resolution.outcome,
            status=resolution.status,
            resolved_by=resolution.resolved_by,
            source_reference=resolution.source_reference,
            supersedes_id=resolution.supersedes_id,
            notes=resolution.notes,
            resolved_at=resolution.resolved_at,
        )


class DisputeORM(Base):
    """Таблица ``disputes`` — оспаривания (изменяемый жизненный цикл)."""

    __tablename__ = "disputes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False, index=True
    )
    resolution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resolutions.id"), nullable=False
    )
    raised_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[DisputeStatus] = mapped_column(
        _dispute_status_enum, nullable=False, index=True
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    decision_notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )

    def to_domain(self) -> Dispute:
        """ORM → доменная сущность."""
        return Dispute(
            id=self.id,
            event_id=self.event_id,
            resolution_id=self.resolution_id,
            raised_by=self.raised_by,
            reason=self.reason,
            evidence=self.evidence,
            status=self.status,
            decided_by=self.decided_by,
            decision_notes=self.decision_notes,
            created_at=self.created_at,
            decided_at=self.decided_at,
        )

    @classmethod
    def from_domain(cls, dispute: Dispute) -> DisputeORM:
        """Доменная сущность → новая ORM-строка."""
        return cls(
            id=dispute.id,
            event_id=dispute.event_id,
            resolution_id=dispute.resolution_id,
            raised_by=dispute.raised_by,
            reason=dispute.reason,
            evidence=dispute.evidence,
            status=dispute.status,
            decided_by=dispute.decided_by,
            decision_notes=dispute.decision_notes,
            created_at=dispute.created_at,
            decided_at=dispute.decided_at,
        )


class ScoringDispatchORM(Base):
    """Таблица ``resolution_scoring_dispatches`` — маркеры поставленного скоринга."""

    __tablename__ = "resolution_scoring_dispatches"

    resolution_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("resolutions.id"), primary_key=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False
    )
    dispatched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
