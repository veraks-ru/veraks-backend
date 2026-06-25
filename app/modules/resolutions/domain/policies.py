"""Доменные политики resolutions: RBAC и разделение обязанностей.

Чистые проверки прав: либо проходят молча, либо поднимают доменную ошибку
(маппится в 403). Используются прикладным слоем (use-cases), а не роутером.
"""

from __future__ import annotations

import uuid

from app.modules.identity.domain.entities import UserRole
from app.modules.resolutions.domain.errors import (
    ResolutionPermissionError,
    SelfDisputeDecisionError,
)

# Кто вправе фиксировать/пересматривать исход события.
_RESOLVE_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.EDITOR, UserRole.ARBITER, UserRole.ADMIN}
)
# Кто вправе выносить решение по спору (арбитраж).
_ARBITRATE_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.ARBITER, UserRole.ADMIN}
)


def ensure_can_resolve(role: UserRole) -> None:
    """Фиксация исхода — редактор/арбитр/админ."""
    if role not in _RESOLVE_ROLES:
        raise ResolutionPermissionError(
            "Фиксация исхода доступна только редактору, арбитру или администратору"
        )


def ensure_can_raise_dispute(role: UserRole) -> None:
    """Оспаривание доступно любому аутентифицированному пользователю.

    Содержательный фильтр «только участник» — отдельная проверка через
    ``ParticipationGateway`` в use-case; роль здесь не ограничиваем
    (редактор/арбитр тоже могут быть участниками).
    """
    return None


def ensure_can_decide_dispute(role: UserRole) -> None:
    """Решение по спору — арбитр/админ."""
    if role not in _ARBITRATE_ROLES:
        raise ResolutionPermissionError(
            "Решение по спору доступно только арбитру или администратору"
        )


def ensure_not_self_decision(*, decided_by: uuid.UUID, raised_by: uuid.UUID) -> None:
    """Нельзя решать собственный спор (разделение обязанностей)."""
    if decided_by == raised_by:
        raise SelfDisputeDecisionError("Нельзя выносить решение по собственному спору")
