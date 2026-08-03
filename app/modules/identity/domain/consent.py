"""Доменные сущности согласий на обработку ПДн и принятие оферты (152-ФЗ).

При первом входе пользователь обязан принять актуальные версии обязательных
документов (оферта, согласие на обработку ПДн). Реестр обязательных
документов и их текущих версий — конфигурация (``app/config.py``,
``ConsentsSettings``); юрист меняет версию через env, и это тут же делает
согласие пользователя недостаточным (см. ``domain/policies.py``).

Хранение — append-only (``user_consents``, триггер ``block_mutations()``):
факт принятия конкретной версии не редактируется и не удаляется, только
добавляется новая строка при принятии новой версии.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ConsentDocument:
    """Обязательный документ и его версия (запись реестра из настроек)."""

    document: str
    version: str


@dataclass(slots=True)
class Consent:
    """Факт принятия пользователем конкретной версии документа.

    Идемпотентность обеспечивается на уровне хранилища —
    ``UNIQUE(user_id, document, version)``: повторное принятие той же версии
    не создаёт вторую строку.
    """

    user_id: uuid.UUID
    document: str
    version: str
    method: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    accepted_at: datetime = field(default_factory=_utcnow)
    ip: str | None = None
    user_agent: str | None = None

    def satisfies(self, required: ConsentDocument) -> bool:
        """Покрывает ли это согласие обязательный документ (тот же документ и версия)."""
        return self.document == required.document and self.version == required.version
