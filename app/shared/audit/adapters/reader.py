"""Реализация :class:`AuditLogReader` поверх async SQLAlchemy."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.audit.adapters.orm import AuditLogORM
from app.shared.audit.domain.entities import AuditEntry

# Размер пачки для потокового чтения при верификации — компромисс между
# числом round-trip'ов к БД и пиковой памятью (объём демо/MVP это позволяет).
_STREAM_BATCH_SIZE = 500


class SqlAlchemyAuditLogReader:
    """Чтение ``audit_log`` для верификации цепочки и админского списка."""

    def __init__(self, session: AsyncSession, *, batch_size: int = _STREAM_BATCH_SIZE) -> None:
        self._session = session
        # Настраиваемый размер пачки — по умолчанию боевой, юнит-тесты границы
        # пагинации (см. tests/shared/audit/unit/test_reader_batching.py)
        # уменьшают его, чтобы не гонять реальную БД с 500+ записями.
        self._batch_size = batch_size

    async def stream_ordered(self) -> AsyncIterator[AuditEntry]:
        """Отдаёт все записи по возрастанию ``id`` пачками, не грузя всё разом."""
        last_id = 0
        while True:
            rows = (
                (
                    await self._session.execute(
                        select(AuditLogORM)
                        .where(AuditLogORM.id > last_id)
                        .order_by(AuditLogORM.id.asc())
                        .limit(self._batch_size)
                    )
                )
                .scalars()
                .all()
            )
            if not rows:
                return
            for row in rows:
                yield row.to_domain()
            last_id = rows[-1].id

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
        """Страница записей по убыванию ``id`` с опциональными фильтрами."""
        stmt = select(AuditLogORM).order_by(AuditLogORM.id.desc()).limit(limit)
        if action is not None:
            stmt = stmt.where(AuditLogORM.action == action)
        if actor_id is not None:
            stmt = stmt.where(AuditLogORM.actor_id == actor_id)
        if occurred_from is not None:
            stmt = stmt.where(AuditLogORM.occurred_at >= occurred_from)
        if occurred_to is not None:
            stmt = stmt.where(AuditLogORM.occurred_at <= occurred_to)
        if before_id is not None:
            stmt = stmt.where(AuditLogORM.id < before_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]
