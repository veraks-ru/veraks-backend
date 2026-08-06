"""Постраничное чтение аудит-журнала для админки."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.shared.audit.domain.entities import AuditEntry
from app.shared.audit.ports.audit_reader import AuditLogReader


@dataclass(frozen=True, slots=True)
class AuditLogPage:
    """Страница журнала: записи + признак наличия следующей страницы."""

    items: Sequence[AuditEntry]
    has_more: bool


class ListAuditLog:
    """Список записей аудита с фильтрами и keyset-пагинацией (новые сначала)."""

    def __init__(self, *, reader: AuditLogReader) -> None:
        self._reader = reader

    async def execute(
        self,
        *,
        action: str | None = None,
        actor_id: uuid.UUID | None = None,
        occurred_from: datetime | None = None,
        occurred_to: datetime | None = None,
        before_id: int | None = None,
        limit: int = 50,
    ) -> AuditLogPage:
        """Отдаёт страницу; ``before_id`` — курсор «показать ещё»."""
        # На одну запись больше лимита — так узнаём про следующую страницу без
        # отдельного ``COUNT(*)`` по потенциально большой таблице.
        rows = await self._reader.list_page(
            action=action,
            actor_id=actor_id,
            occurred_from=occurred_from,
            occurred_to=occurred_to,
            before_id=before_id,
            limit=limit + 1,
        )
        has_more = len(rows) > limit
        return AuditLogPage(items=list(rows[:limit]), has_more=has_more)
