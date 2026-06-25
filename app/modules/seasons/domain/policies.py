"""Доменные RBAC-политики сезонов — чистые правила доступа без I/O.

Разделение обязанностей (см. раздел безопасности PRD): заводить и править
сезоны может редакция (editor/admin), а переводить статус (активация и —
особенно — финализация, момент определения призёров) — только администратор.
"""

from __future__ import annotations

from app.modules.identity.domain.entities import UserRole
from app.modules.seasons.domain.errors import SeasonPermissionError

_MANAGE_ROLES: frozenset[UserRole] = frozenset({UserRole.EDITOR, UserRole.ADMIN})


def ensure_can_manage_seasons(role: UserRole) -> None:
    """Требует роль редактора/администратора для создания/правки сезона."""
    if role not in _MANAGE_ROLES:
        raise SeasonPermissionError("Недостаточно прав для управления сезонами")


def ensure_can_transition(role: UserRole) -> None:
    """Требует роль администратора для перевода статуса сезона."""
    if role is not UserRole.ADMIN:
        raise SeasonPermissionError("Перевод статуса сезона доступен только админу")
