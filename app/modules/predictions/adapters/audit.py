"""Адаптер аудита — временный приёмник истории прогнозов.

TODO(audit-integration): заменить на запись в append-only ``audit_log`` с
hash-цепочкой (см. модель данных §2.6). Пока что лишь структурно логирует
изменение, чтобы контракт порта соблюдался и поведение было наблюдаемым в
интеграции, не блокируя домен прогнозов до готовности домена аудита.
"""

from __future__ import annotations

import logging

from app.modules.predictions.application.dto import PredictionAuditEntry

logger = logging.getLogger("orakul.predictions.audit")


class LoggingAuditRecorder:
    """Пишет запись истории прогноза в журнал приложения (заглушка)."""

    async def record(self, entry: PredictionAuditEntry) -> None:
        """Логирует переход градации прогноза."""
        logger.info(
            "%s prediction=%s event=%s actor=%s grade=%s→%s",
            entry.action,
            entry.prediction_id,
            entry.event_id,
            entry.actor_id,
            entry.before,
            entry.after,
        )
