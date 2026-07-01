"""Шлюз проверки существования события (шов к домену events)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.events.adapters.orm import EventORM


class SqlAlchemyEventExistsGateway:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, event_id: uuid.UUID) -> bool:
        stmt = select(EventORM.id).where(EventORM.id == event_id).limit(1)
        return (await self._session.execute(stmt)).first() is not None

    async def creator_id(self, event_id: uuid.UUID) -> uuid.UUID | None:
        stmt = select(EventORM.created_by).where(EventORM.id == event_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()
