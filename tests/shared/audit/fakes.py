"""In-memory фейк порта чтения аудита + билдер валидной тестовой цепочки."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

from app.shared.audit.domain.entities import AuditActorType, AuditEntry
from app.shared.audit.domain.hashing import chain_hash, entry_payload

FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build_valid_chain(n: int) -> list[AuditEntry]:
    """Строит цепочку из ``n`` записей с корректно посчитанными хешами."""
    entries: list[AuditEntry] = []
    prev_hash: str | None = None
    for i in range(1, n + 1):
        after = {"n": i}
        payload = entry_payload(
            occurred_at=FIXED_NOW,
            actor_id=None,
            actor_type=AuditActorType.SYSTEM,
            action=f"test.action.{i}",
            entity_type="test",
            entity_id=None,
            before=None,
            after=after,
            metadata={},
        )
        digest = chain_hash(prev_hash, payload)
        entries.append(
            AuditEntry(
                occurred_at=FIXED_NOW,
                actor_id=None,
                actor_type=AuditActorType.SYSTEM,
                action=f"test.action.{i}",
                entity_type="test",
                entity_id=None,
                hash=digest,
                before=None,
                after=after,
                metadata={},
                prev_hash=prev_hash,
                id=i,
            )
        )
        prev_hash = digest
    return entries


class FakeAuditLogReader:
    """In-memory реализация :class:`AuditLogReader` над заранее заданным списком."""

    def __init__(self, entries: list[AuditEntry]) -> None:
        self._entries = entries

    async def stream_ordered(self) -> AsyncIterator[AuditEntry]:
        for entry in sorted(self._entries, key=lambda e: e.id or 0):
            yield entry

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
        rows = sorted(self._entries, key=lambda e: e.id or 0, reverse=True)
        if action is not None:
            rows = [r for r in rows if r.action == action]
        if actor_id is not None:
            rows = [r for r in rows if r.actor_id == actor_id]
        if occurred_from is not None:
            rows = [r for r in rows if r.occurred_at >= occurred_from]
        if occurred_to is not None:
            rows = [r for r in rows if r.occurred_at <= occurred_to]
        if before_id is not None:
            rows = [r for r in rows if (r.id or 0) < before_id]
        return rows[:limit]
