"""Реализация :class:`AuditTrail` поверх async SQLAlchemy.

Запись звена сериализуется транзакционным advisory-локом
(``pg_advisory_xact_lock``): под ним читается последний ``hash``, считается
новый и вставляется строка. Это исключает гонку, при которой два конкурентных
писателя возьмут один и тот же ``prev_hash`` и разорвут цепочку. Лок снимается
автоматически в конце транзакции.

Источник времени — сервер: ``occurred_at`` берётся здесь как ``now(UTC)``.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import session_scope
from app.shared.audit.adapters.orm import AuditLogORM
from app.shared.audit.domain.entities import AuditActorType, AuditEntry
from app.shared.audit.domain.hashing import chain_hash, entry_payload

# Произвольная, но фиксированная константа advisory-лока для цепочки audit_log.
_AUDIT_CHAIN_LOCK_KEY = 0x4155_4449_5400  # "AUDIT\0"


class SqlAlchemyAuditTrail:
    """Append-only журнал с хеш-цепочкой поверх таблицы ``audit_log``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        """Считает звено цепочки под advisory-локом и вставляет строку."""
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": _AUDIT_CHAIN_LOCK_KEY},
        )
        prev_hash = (
            await self._session.execute(
                select(AuditLogORM.hash).order_by(AuditLogORM.id.desc()).limit(1)
            )
        ).scalar_one_or_none()

        occurred_at = datetime.now(timezone.utc)
        meta = dict(metadata or {})
        payload = entry_payload(
            occurred_at=occurred_at,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=before,
            after=after,
            metadata=meta,
        )
        digest = chain_hash(prev_hash, payload)

        orm = AuditLogORM(
            occurred_at=occurred_at,
            actor_id=actor_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            before=dict(before) if before is not None else None,
            after=dict(after) if after is not None else None,
            meta=meta,
            prev_hash=prev_hash,
            hash=digest,
        )
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()


class ImmediatelyCommittingAuditTrail:
    """``AuditTrail``, коммитящий каждую запись в СВОЕЙ короткой транзакции.

    Обычный ``SqlAlchemyAuditTrail`` делит сессию (и транзакцию) с вызывающим
    use-case — это правильно по умолчанию: запись аудита коммитится атомарно
    вместе с изменением состояния (§6.2), а откат операции откатывает и её.

    Но для события БЕЗОПАСНОСТИ, которое пишется НЕПОСРЕДСТВЕННО ПЕРЕД тем,
    как use-case намеренно поднимет исключение (детект повторного
    использования refresh-токена — операция обязана провалиться), это
    поведение — баг: FastAPI-зависимость ``get_session`` делает ``rollback``
    всей транзакции запроса при любом исключении, и запись об инциденте
    исчезает вместе с ней, хотя именно она должна пережить провал.

    Каждый вызов ``record`` открывает отдельную сессию через
    ``session_scope`` (тот же примитив, что использует воркер вне
    request-scope) и коммитит её сразу по завершении записи — до того, как
    управление вернётся к вызывающему коду. Это делает запись независимой от
    судьбы «внешней» транзакции запроса: сколько бы она ни откатывалась
    дальше, эта запись уже зафиксирована в БД. Композит-рут выбирает эту
    реализацию точечно — там, где use-case пишет аудит прямо перед `raise`
    (см. ``identity.api.dependencies.get_security_audit_trail``).
    """

    async def record(
        self,
        *,
        actor_id: uuid.UUID | None,
        actor_type: AuditActorType,
        action: str,
        entity_type: str,
        entity_id: uuid.UUID | None,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AuditEntry:
        """Пишет и коммитит запись в своей транзакции; возвращает сохранённое звено."""
        async with session_scope() as session:
            return await SqlAlchemyAuditTrail(session).record(
                actor_id=actor_id,
                actor_type=actor_type,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                before=before,
                after=after,
                metadata=metadata,
            )
