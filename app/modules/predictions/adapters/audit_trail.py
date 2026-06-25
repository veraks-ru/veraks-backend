"""Боевой адаптер аудита прогнозов поверх общего append-only журнала.

Реализует порт ``AuditRecorder`` домена прогнозов, делегируя записи в общий
``audit_log`` с hash-цепочкой (``app.shared.audit``, модель данных §2.6). Так
история правок прогноза (до блокировки) становится неизменяемой и проверяемой,
а не теряется в логах приложения, как во временной ``LoggingAuditRecorder``.
"""

from __future__ import annotations

from app.modules.predictions.application.dto import PredictionAuditEntry
from app.shared.audit.domain.entities import AuditActorType
from app.shared.audit.ports.audit_trail import AuditTrail


class AuditTrailRecorder:
    """Пишет историю прогнозов в общий журнал с хеш-цепочкой."""

    def __init__(self, trail: AuditTrail) -> None:
        self._trail = trail

    async def record(self, entry: PredictionAuditEntry) -> None:
        """Фиксирует переход градации прогноза неизменяемой записью.

        Действие совершает сам участник, поэтому ``actor_type = USER``. Сервер —
        источник времени: ``occurred_at``/``prev_hash``/``hash`` вычисляет журнал.
        """
        await self._trail.record(
            actor_id=entry.actor_id,
            actor_type=AuditActorType.USER,
            action=entry.action,
            entity_type="prediction",
            entity_id=entry.prediction_id,
            before={"grade": entry.before} if entry.before is not None else None,
            after={"grade": entry.after},
            metadata={"event_id": str(entry.event_id)},
        )
