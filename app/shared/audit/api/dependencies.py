"""Composition root общей инфраструктуры аудита (FastAPI DI).

Единственное место, где порт чтения журнала связывается с SQL-адаптером и где
проверяется RBAC (только ``admin`` — журнал целиком, включая payload'ы других
пользователей, чувствителен).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.modules.identity.api.dependencies import CurrentUser
from app.modules.identity.domain.entities import User, UserRole
from app.shared.audit.adapters.reader import SqlAlchemyAuditLogReader
from app.shared.audit.application.list_log import ListAuditLog
from app.shared.audit.application.verify_chain import VerifyAuditChain
from app.shared.audit.ports.audit_reader import AuditLogReader

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_audit_log_reader(session: SessionDep) -> AuditLogReader:
    """Читатель аудит-журнала поверх текущей сессии."""
    return SqlAlchemyAuditLogReader(session)


AuditReaderDep = Annotated[AuditLogReader, Depends(get_audit_log_reader)]


def require_admin(current_user: CurrentUser) -> User:
    """Гард: аудит-журнал и его верификация — только администратору.

    Записи содержат ``before``/``after`` любых пользователей и действий
    editor/arbiter — это не для их собственных ролей, а строго для admin.
    """
    if current_user.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Аудит-журнал доступен только администратору",
        )
    return current_user


AdminUser = Annotated[User, Depends(require_admin)]


def get_list_audit_log(reader: AuditReaderDep) -> ListAuditLog:
    """Use-case постраничного чтения журнала."""
    return ListAuditLog(reader=reader)


def get_verify_audit_chain(reader: AuditReaderDep) -> VerifyAuditChain:
    """Use-case верификации хеш-цепочки."""
    return VerifyAuditChain(reader=reader)
