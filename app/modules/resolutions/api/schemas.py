"""Pydantic-схемы запросов/ответов resolutions (тонкий транспортный слой).

Доменные сущности маппятся в ответы через ``from_domain``; реальное имя/PII в
ответы не попадают (здесь их и нет). Валидация входа — на уровне pydantic;
доменные инварианты проверяются в use-cases.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.modules.resolutions.domain.entities import (
    Dispute,
    DisputeStatus,
    Resolution,
    ResolutionStatus,
)


class FixResolutionRequest(BaseModel):
    """Фиксация исхода события (editor/arbiter/admin)."""

    outcome: bool = Field(description="Бинарный исход события: Да (true) / Нет (false)")
    source_reference: str = Field(
        min_length=1, description="Ссылка на доказательство из заданного источника"
    )
    notes: str = Field(default="", description="Произвольные примечания")


class ResolutionResponse(BaseModel):
    """Проекция решения по событию."""

    id: uuid.UUID
    event_id: uuid.UUID
    outcome: bool
    status: ResolutionStatus
    resolved_by: uuid.UUID
    source_reference: str
    supersedes_id: uuid.UUID | None
    notes: str
    resolved_at: datetime

    @classmethod
    def from_domain(cls, resolution: Resolution) -> "ResolutionResponse":
        """Доменная сущность → схема ответа."""
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


class RaiseDisputeRequest(BaseModel):
    """Подача оспаривания участником."""

    reason: str = Field(min_length=1, description="Причина оспаривания")
    evidence: str = Field(default="", description="Доказательства/ссылки")


class DecideDisputeRequest(BaseModel):
    """Решение арбитра по спору."""

    accept: bool = Field(description="Удовлетворить (true) или отклонить (false)")
    decision_notes: str = Field(default="", description="Обоснование решения")
    new_outcome: bool | None = Field(
        default=None,
        description="Новый исход при удовлетворении спора (overturn); обязателен при accept=true",
    )


class DisputeResponse(BaseModel):
    """Проекция спора по событию."""

    id: uuid.UUID
    event_id: uuid.UUID
    resolution_id: uuid.UUID
    raised_by: uuid.UUID
    reason: str
    evidence: str
    status: DisputeStatus
    decided_by: uuid.UUID | None
    decision_notes: str
    created_at: datetime
    decided_at: datetime | None

    @classmethod
    def from_domain(cls, dispute: Dispute) -> "DisputeResponse":
        """Доменная сущность → схема ответа."""
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
