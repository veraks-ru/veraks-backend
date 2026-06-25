"""SQLAlchemy-реализация ``UserRepository``."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import User
from app.modules.identity.ports.repositories import (
    SnilsAlreadyExistsError,
    UsernameTakenError,
)


class SqlAlchemyUserRepository:
    """Хранилище пользователей поверх асинхронной сессии SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        """Аккаунт по PK."""
        orm = await self._session.get(UserORM, user_id)
        return orm.to_domain() if orm else None

    async def get_by_snils_hash(self, snils_hash: str) -> User | None:
        """Аккаунт по HMAC-хешу СНИЛС."""
        stmt = select(UserORM).where(UserORM.snils_hash == snils_hash)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def get_by_esia_oid(self, esia_oid: str) -> User | None:
        """Аккаунт по идентификатору ЕСИА."""
        stmt = select(UserORM).where(UserORM.esia_oid == esia_oid)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def username_exists(self, username: str) -> bool:
        """Занятость хэндла (citext — регистронезависимо)."""
        stmt = select(func.count()).select_from(UserORM).where(
            UserORM.username == username
        )
        return bool((await self._session.execute(stmt)).scalar_one())

    async def add(self, user: User) -> User:
        """Вставляет нового пользователя, разбирая нарушения UNIQUE."""
        orm = UserORM.from_domain(user)
        self._session.add(orm)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint = _constraint_name(exc)
            if constraint and "snils_hash" in constraint:
                raise SnilsAlreadyExistsError(str(exc)) from exc
            if constraint and "username" in constraint:
                raise UsernameTakenError(str(exc)) from exc
            raise
        return orm.to_domain()

    async def update(self, user: User) -> User:
        """Обновляет изменяемые поля существующего аккаунта."""
        orm = await self._session.get(UserORM, user.id)
        if orm is None:  # pragma: no cover — вызывается только для существующих
            raise SnilsAlreadyExistsError("Аккаунт исчез во время обновления")
        orm.esia_oid = user.esia_oid
        orm.username = user.username
        orm.display_name = user.display_name
        orm.real_name_enc = user.real_name_enc
        orm.role = user.role
        orm.status = user.status
        await self._session.flush()
        return orm.to_domain()


def _constraint_name(exc: IntegrityError) -> str | None:
    """Достаёт имя нарушенного ограничения из исключения драйвера."""
    constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
    if constraint:
        return str(constraint)
    return str(exc.orig)
