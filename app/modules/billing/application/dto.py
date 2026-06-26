"""DTO прикладного слоя billing (frozen dataclass'ы, не pydantic)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.modules.billing.domain.entities import Payout, PrizeFund
from app.modules.billing.domain.ledger import LedgerType
from app.modules.identity.domain.entities import UserRole


@dataclass(frozen=True, slots=True)
class LedgerReconciliation:
    """Итог сверки одной кассы: суммы дебетов/кредитов и сходится ли баланс.

    Двойная запись требует ``debit == credit`` по кассе целиком; расхождение —
    признак повреждения данных в обход триггеров (требует расследования).
    """

    ledger_type: LedgerType
    total_debit_kopecks: int
    total_credit_kopecks: int

    @property
    def balanced(self) -> bool:
        return self.total_debit_kopecks == self.total_credit_kopecks


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


@dataclass(frozen=True, slots=True)
class SeasonPrizeFundView:
    """Прозрачность по сезону: его фонды (с сальдо) и история выплат."""

    season_slug: str
    funds: list[PrizeFundView]
    payouts: list[Payout]
