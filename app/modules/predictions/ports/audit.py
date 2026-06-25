"""Порт аудита — запись истории изменений прогноза.

История правок прогноза (до блокировки) фиксируется append-only в общий
``audit_log`` (см. модель данных §2.6: сам прогноз хранится latest-wins, а
история — в журнале аудита). Домен прогнозов зависит лишь от этого контракта.

TODO(audit-integration): подключить реальный адаптер к ``audit_log`` с
hash-цепочкой, когда домен аудита будет реализован.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.modules.predictions.application.dto import PredictionAuditEntry


@runtime_checkable
class AuditRecorder(Protocol):
    """Приёмник записей истории прогнозов."""

    async def record(self, entry: PredictionAuditEntry) -> None:
        """Сохраняет запись аудита (append-only)."""
        ...
