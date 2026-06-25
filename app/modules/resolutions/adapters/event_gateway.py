"""Шлюз resolutions к домену events (поверх таблицы ``events``).

Единственная точка смены статуса события из resolutions. Переходы исполняются
методами доменной сущности ``Event`` (она владеет конечным автоматом и
проверяет допустимость), а сюда лишь сохраняются изменённые колонки. Это
сохраняет инварианты events и даёт ``InvalidEventTransitionError`` при
некорректном переходе.

TODO(resolutions-integration): прямое чтение/запись таблицы соседнего домена в
монолите; заменить сетевым контрактом при выделении events в отдельный сервис.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import EventORM
from app.modules.events.domain.entities import Event, EventStatus
from app.modules.resolutions.application.dto import EventLifecycle
from app.modules.resolutions.domain.errors import (
    ResolutionTargetEventNotFoundError,
)


class SqlAlchemyEventResolutionGateway:
    """Чтение статуса события и драйв переходов автомата events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_lifecycle(self, event_id: uuid.UUID) -> EventLifecycle | None:
        """Срез жизненного цикла события или ``None``."""
        orm = await self._session.get(EventORM, event_id)
        if orm is None:
            return None
        return EventLifecycle(
            event_id=orm.id,
            status=orm.status,
            outcome=orm.outcome,
            dispute_window_ends_at=orm.dispute_window_ends_at,
            season_id=orm.season_id,
        )

    async def fix_outcome(
        self,
        event_id: uuid.UUID,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime,
    ) -> None:
        """``closed → resolving → resolved``: фиксирует исход и открывает окно."""
        orm, event = await self._load(event_id)
        event.begin_resolution(now=now)
        event.record_outcome(
            outcome=outcome, dispute_window_ends_at=dispute_window_ends_at, now=now
        )
        await self._persist(orm, event)

    async def open_dispute(self, event_id: uuid.UUID, *, now: datetime) -> None:
        """``resolved → disputed``."""
        orm, event = await self._load(event_id)
        event.open_dispute(now=now)
        await self._persist(orm, event)

    async def dismiss_dispute(self, event_id: uuid.UUID, *, now: datetime) -> None:
        """``disputed → resolved`` без изменения исхода/окна (спор отклонён)."""
        orm, event = await self._load(event_id)
        event.dismiss_dispute(now=now)
        await self._persist(orm, event)

    async def overturn_outcome(
        self,
        event_id: uuid.UUID,
        *,
        outcome: bool,
        dispute_window_ends_at: datetime,
        now: datetime,
    ) -> None:
        """``disputed → resolved`` с новым исходом и заново открытым окном."""
        orm, event = await self._load(event_id)
        event.record_outcome(
            outcome=outcome, dispute_window_ends_at=dispute_window_ends_at, now=now
        )
        await self._persist(orm, event)

    async def find_resolved_past_window(self, *, now: datetime) -> list[uuid.UUID]:
        """ID ``resolved``-событий с истёкшим окном оспаривания."""
        stmt = select(EventORM.id).where(
            EventORM.status == EventStatus.RESOLVED,
            EventORM.dispute_window_ends_at.is_not(None),
            EventORM.dispute_window_ends_at <= now,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return list(rows)

    async def _load(self, event_id: uuid.UUID) -> tuple[EventORM, Event]:
        """Загружает строку события и доменную сущность (или 404)."""
        orm = await self._session.get(EventORM, event_id)
        if orm is None:
            raise ResolutionTargetEventNotFoundError("Событие не найдено")
        return orm, orm.to_domain()

    async def _persist(self, orm: EventORM, event: Event) -> None:
        """Переносит изменённые доменом поля жизненного цикла в ORM-строку."""
        orm.status = event.status
        orm.outcome = event.outcome
        orm.resolved_at = event.resolved_at
        orm.dispute_window_ends_at = event.dispute_window_ends_at
        orm.updated_at = event.updated_at
        await self._session.flush()
