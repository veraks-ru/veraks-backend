"""Доменные политики events — чистые правила доступа без I/O.

Разделение обязанностей (см. раздел безопасности задания): создавать и
вести события вправе только редакция. RBAC-роль — общий «kernel» с доменом
identity: events переиспользует :class:`UserRole`, а не дублирует enum ролей.
"""

from __future__ import annotations

from app.modules.events.domain.errors import EventPermissionError

# Общий справочник ролей живёт в identity (shared kernel RBAC).
from app.modules.identity.domain.entities import UserRole

# Роли, которым разрешено управлять событиями (CRUD, публикация, закрытие).
_EVENT_MANAGER_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.EDITOR, UserRole.ADMIN}
)


def ensure_can_manage_events(role: UserRole) -> None:
    """Требует роль редактора/администратора для операций над событиями.

    Поднимает :class:`EventPermissionError`, если роль недостаточна.
    """
    if role not in _EVENT_MANAGER_ROLES:
        raise EventPermissionError(
            "Операция доступна только редактору или администратору"
        )
