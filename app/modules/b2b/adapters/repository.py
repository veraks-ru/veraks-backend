"""SQLAlchemy-репозиторий API-ключей."""

from __future__ import annotations

import uuid

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.b2b.adapters.orm import ApiKeyORM
from app.modules.b2b.domain.entities import ApiKey


class SqlAlchemyApiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, key: ApiKey) -> ApiKey:
        orm = ApiKeyORM.from_domain(key)
        self._session.add(orm)
        await self._session.flush()
        return orm.to_domain()

    async def get_by_id(self, key_id: uuid.UUID) -> ApiKey | None:
        orm = await self._session.get(ApiKeyORM, key_id)
        return orm.to_domain() if orm is not None else None

    async def get_active_by_hash(self, key_hash: str) -> ApiKey | None:
        stmt = select(ApiKeyORM).where(
            ApiKeyORM.key_hash == key_hash,
            ApiKeyORM.is_active.is_(True),
        )
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm is not None else None

    async def list_for_owner(
        self, owner_user_id: uuid.UUID
    ) -> list[ApiKey]:
        stmt = (
            select(ApiKeyORM)
            .where(ApiKeyORM.owner_user_id == owner_user_id)
            .order_by(ApiKeyORM.created_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [r.to_domain() for r in rows]

    async def update(self, key: ApiKey) -> ApiKey:
        await self._session.execute(
            update(ApiKeyORM)
            .where(ApiKeyORM.id == key.id)
            .values(is_active=key.is_active, revoked_at=key.revoked_at)
        )
        return key
