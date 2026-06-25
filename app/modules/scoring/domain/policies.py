"""Доменные политики scoring — чистые правила доступа без I/O.

Скоринг и пересчёт рейтингов — операционные действия (фон/админ), а не
пользовательские. Разделение обязанностей (см. раздел безопасности задания):
запуск скоринга события — редакция/арбитр/администратор; полный пересчёт
рейтингов — только администратор. RBAC-роль — общий kernel с identity.
"""

from __future__ import annotations

from app.modules.identity.domain.entities import UserRole
from app.modules.scoring.domain.errors import ScoringPermissionError

# Роли, которым разрешено инициировать скоринг события.
_SCORING_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.EDITOR, UserRole.ARBITER, UserRole.ADMIN}
)


def ensure_can_score(role: UserRole) -> None:
    """Требует роль редактора/арбитра/администратора для запуска скоринга."""
    if role not in _SCORING_ROLES:
        raise ScoringPermissionError(
            "Запуск скоринга доступен только редактору, арбитру или администратору"
        )


def ensure_can_recompute(role: UserRole) -> None:
    """Требует роль администратора для полного пересчёта рейтингов."""
    if role is not UserRole.ADMIN:
        raise ScoringPermissionError(
            "Полный пересчёт рейтингов доступен только администратору"
        )
