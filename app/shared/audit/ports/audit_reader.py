"""Порт чтения неизменяемого аудит-журнала.

Отделён от :class:`~app.shared.audit.ports.audit_trail.AuditTrail` (только
запись): чтение нужно верификации цепочки и админке, писать в журнал им не
требуется — узкие порты снижают связность.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.shared.audit.domain.entities import AuditEntry


@runtime_checkable
class AuditLogReader(Protocol):
    """Чтение записей ``audit_log``."""

    def stream_ordered(self) -> AsyncIterator[AuditEntry]:
        """Все записи по возрастанию ``id`` — порядок цепочки для верификации."""
        ...

    async def list_page(
        self,
        *,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> Sequence[AuditEntry]:
        """Страница записей по убыванию ``id`` (новые сначала) с фильтрами.

        ``before_id`` — keyset-курсор (строго меньше этого id) для «показать
        ещё»; без него — первая страница. Отдаёт ``limit`` записей.
        """
        ...
