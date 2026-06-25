"""DTO прикладного слоя billing (frozen dataclass'ы, не pydantic)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.billing.domain.entities import PrizeFund
from app.modules.identity.domain.entities import UserRole


@dataclass(frozen=True, slots=True)
class Actor:
    """Актор операции: идентификатор пользователя и его роль (RBAC/SoD)."""

    user_id: uuid.UUID
    role: UserRole


@dataclass(frozen=True, slots=True)
class PrizeFundView:
    """Проекция фонда для прозрачности: сам фонд + фактическое сальдо кассы."""

    fund: PrizeFund
    balance_kopecks: int
