"""Доменные сущности resolutions: ``Resolution`` и ``Dispute``.

Обычные dataclass'ы без инфраструктуры. ``Resolution`` неизменяема (журнал
append-only: пересмотр — новая строка через ``supersedes_id``), поэтому у неё
только фабрики, без мутаторов. ``Dispute`` имеет управляемый жизненный цикл
(``open → under_review → accepted|rejected``) с проверкой переходов.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.resolutions.domain.errors import (
    DisputeAlreadyDecidedError,
    InvalidResolutionDataError,
)


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


def _require_text(raw: str, *, field_name: str, max_length: int = 10_000) -> str:
    """Проверяет обязательное текстовое поле и возвращает обрезанное значение."""
    value = raw.strip()
    if not value:
        raise InvalidResolutionDataError(
            f"Поле «{field_name}» обязательно и не может быть пустым"
        )
    if len(value) > max_length:
        raise InvalidResolutionDataError(
            f"Поле «{field_name}» превышает {max_length} символов"
        )
    return value


class ResolutionStatus(str, enum.Enum):
    """Статус решения.

    MVP фиксирует исход одношагово — пишется сразу ``final``. ``proposed``
    зарезервирован под будущий двухшаговый maker-checker (редактор предлагает,
    арбитр подтверждает). ``overturned`` сохранён для полноты схемы; в журнале
    «отменённость» восстанавливается по цепочке ``supersedes_id`` (без UPDATE).
    """

    PROPOSED = "proposed"
    FINAL = "final"
    OVERTURNED = "overturned"


class DisputeStatus(str, enum.Enum):
    """Жизненный цикл спора."""

    OPEN = "open"
    UNDER_REVIEW = "under_review"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


_OPEN_DISPUTE_STATUSES: frozenset[DisputeStatus] = frozenset(
    {DisputeStatus.OPEN, DisputeStatus.UNDER_REVIEW}
)


@dataclass(slots=True)
class Resolution:
    """Решение по событию — строка неизменяемого журнала ``resolutions``.

    «Текущее» решение события = последняя строка ``status=final``. Пересмотр
    (overturn по принятому спору) — новая ``final``-строка с ``supersedes_id``,
    указывающим на отменяемое решение; прежняя строка остаётся в журнале.
    """

    event_id: uuid.UUID
    outcome: bool
    resolved_by: uuid.UUID
    source_reference: str
    status: ResolutionStatus = ResolutionStatus.FINAL
    supersedes_id: uuid.UUID | None = None
    notes: str = ""
    resolved_at: datetime = field(default_factory=_utcnow)
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def finalize(
        cls,
        *,
        event_id: uuid.UUID,
        outcome: bool,
        resolved_by: uuid.UUID,
        source_reference: str,
        notes: str = "",
        supersedes_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> Resolution:
        """Создаёт финальное решение (опц. пересматривающее ``supersedes_id``)."""
        return cls(
            event_id=event_id,
            outcome=outcome,
            resolved_by=resolved_by,
            source_reference=_require_text(
                source_reference, field_name="source_reference"
            ),
            status=ResolutionStatus.FINAL,
            supersedes_id=supersedes_id,
            notes=notes.strip(),
            resolved_at=now or _utcnow(),
        )


@dataclass(slots=True)
class Dispute:
    """Оспаривание решения по событию (изменяемый жизненный цикл)."""

    event_id: uuid.UUID
    resolution_id: uuid.UUID
    raised_by: uuid.UUID
    reason: str
    evidence: str = ""
    status: DisputeStatus = DisputeStatus.OPEN
    decided_by: uuid.UUID | None = None
    decision_notes: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    decided_at: datetime | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    @classmethod
    def open_for(
        cls,
        *,
        event_id: uuid.UUID,
        resolution_id: uuid.UUID,
        raised_by: uuid.UUID,
        reason: str,
        evidence: str = "",
        now: datetime | None = None,
    ) -> Dispute:
        """Создаёт открытый спор с провалидированной причиной."""
        return cls(
            event_id=event_id,
            resolution_id=resolution_id,
            raised_by=raised_by,
            reason=_require_text(reason, field_name="reason"),
            evidence=evidence.strip(),
            status=DisputeStatus.OPEN,
            created_at=now or _utcnow(),
        )

    def is_open(self) -> bool:
        """Открыт ли спор (блокирует скоринг/финализацию сезона)."""
        return self.status in _OPEN_DISPUTE_STATUSES

    def decide(
        self,
        *,
        accepted: bool,
        decided_by: uuid.UUID,
        decision_notes: str = "",
        now: datetime | None = None,
    ) -> None:
        """Закрывает спор решением арбитра.

        Поднимает :class:`DisputeAlreadyDecidedError`, если спор уже закрыт
        (нельзя пересматривать решение по спору — только новый спор в пределах
        окна).
        """
        if not self.is_open():
            raise DisputeAlreadyDecidedError(
                f"Спор уже закрыт со статусом «{self.status.value}»"
            )
        self.status = DisputeStatus.ACCEPTED if accepted else DisputeStatus.REJECTED
        self.decided_by = decided_by
        self.decision_notes = decision_notes.strip()
        self.decided_at = now or _utcnow()
