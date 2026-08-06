"""SQLAlchemy-реализация ``UserRepository``."""

from __future__ import annotations

import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity.adapters.orm import ConsentORM, UserORM
from app.modules.identity.domain.consent import Consent
from app.modules.identity.domain.entities import User, UserStatus
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

    async def get_by_esia_oid_hash(self, esia_oid_hash: str) -> User | None:
        """Аккаунт по HMAC-хешу идентификатора ЕСИА."""
        stmt = select(UserORM).where(UserORM.esia_oid_hash == esia_oid_hash)
        orm = (await self._session.execute(stmt)).scalar_one_or_none()
        return orm.to_domain() if orm else None

    async def get_by_username(self, username: str) -> User | None:
        """Аккаунт по публичному хэндлу (citext — регистронезависимо)."""
        stmt = select(UserORM).where(UserORM.username == username)
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
        """Обновляет изменяемые поля существующего аккаунта.

        Смена ``username`` может нарушить ``UNIQUE(username)`` (пользователь
        выбрал занятый хэндл в PATCH /users/me или онбординге) — разбираем
        так же, как в ``add()``.
        """
        orm = await self._session.get(UserORM, user.id)
        if orm is None:  # pragma: no cover — вызывается только для существующих
            raise SnilsAlreadyExistsError("Аккаунт исчез во время обновления")
        orm.esia_oid_hash = user.esia_oid_hash
        orm.username = user.username
        orm.display_name = user.display_name
        orm.real_name_enc = user.real_name_enc
        orm.role = user.role
        orm.status = user.status
        orm.onboarded_at = user.onboarded_at
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            constraint = _constraint_name(exc)
            if constraint and "username" in constraint:
                raise UsernameTakenError(str(exc)) from exc
            raise
        return orm.to_domain()


    async def list_page(
        self,
        *,
        status: UserStatus | None,
        search: str | None,
        limit: int,
        offset: int,
    ) -> tuple[list[User], int]:
        """Страница для админки: фильтр по статусу + ``ILIKE`` по хэндлу/имени.

        ``username`` — уже ``citext`` (регистронезависимый сам по себе), но
        ``ILIKE`` работает и по нему, и по обычному ``display_name`` — единая
        формулировка условия для обоих полей.
        """
        conditions = []
        if status is not None:
            conditions.append(UserORM.status == status)
        if search:
            pattern = f"%{search}%"
            conditions.append(
                or_(UserORM.username.ilike(pattern), UserORM.display_name.ilike(pattern))
            )

        count_stmt = select(func.count()).select_from(UserORM)
        stmt = select(UserORM).order_by(UserORM.created_at.desc())
        for condition in conditions:
            count_stmt = count_stmt.where(condition)
            stmt = stmt.where(condition)
        stmt = stmt.limit(limit).offset(offset)

        total = (await self._session.execute(count_stmt)).scalar_one()
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows], total


def _constraint_name(exc: IntegrityError) -> str | None:
    """Достаёт имя нарушенного ограничения из исключения драйвера."""
    constraint = getattr(getattr(exc.orig, "__cause__", None), "constraint_name", None)
    if constraint:
        return str(constraint)
    return str(exc.orig)


class SqlAlchemyConsentRepository:
    """Хранилище согласий (append-only) поверх асинхронной сессии SQLAlchemy."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: uuid.UUID) -> list[Consent]:
        """Все согласия пользователя, в порядке принятия."""
        stmt = (
            select(ConsentORM)
            .where(ConsentORM.user_id == user_id)
            .order_by(ConsentORM.accepted_at)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [row.to_domain() for row in rows]

    async def add_many(self, consents: list[Consent]) -> None:
        """Вставляет согласия; повторное принятие той же версии — no-op.

        ``ON CONFLICT DO NOTHING`` по ``UNIQUE(user_id, document, version)`` —
        это не UPDATE, поэтому append-only триггер таблицы его не блокирует.
        """
        if not consents:
            return
        values = [
            {
                "id": c.id,
                "user_id": c.user_id,
                "document": c.document,
                "version": c.version,
                "accepted_at": c.accepted_at,
                "method": c.method,
                "ip": c.ip,
                "user_agent": c.user_agent,
            }
            for c in consents
        ]
        stmt = pg_insert(ConsentORM).values(values)
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["user_id", "document", "version"]
        )
        await self._session.execute(stmt)
        await self._session.flush()
