"""Политики доступа billing — чистые функции без I/O.

Разделение обязанностей (SoD): управление призовым фондом и выплатами —
только ``admin``; подтверждение выплаты (checker) обязано отличаться от её
инициатора (maker). Нарушение — специализированная доменная ошибка.
"""

from __future__ import annotations

import uuid

from app.modules.billing.domain.errors import (
    BillingPermissionError,
    SelfApprovalError,
)
from app.modules.identity.domain.entities import UserRole


def ensure_can_manage_prize_funds(role: UserRole) -> None:
    """Заводить призовой фонд может только администратор."""
    if role is not UserRole.ADMIN:
        raise BillingPermissionError("Управление призовым фондом доступно только admin")


def ensure_can_announce_fund(
    *,
    role: UserRole,
    actor_user_id: uuid.UUID,
    sponsor_user_id: uuid.UUID | None,
) -> None:
    """Завести фонд может админ или сам спонсор (свой фонд, self-serve).

    Объявление фонда — декларация без движения денег (проводок нет), поэтому
    безопасно доверить спонсору. Реальное поступление (``RecordSponsorDeposit``)
    и выплаты остаются под отдельными правилами.
    """
    if role is UserRole.ADMIN:
        return
    if sponsor_user_id is not None and actor_user_id == sponsor_user_id:
        return
    raise BillingPermissionError("Завести фонд может админ или сам спонсор")


def ensure_can_deposit_to_fund(
    *,
    role: UserRole,
    actor_user_id: uuid.UUID,
    fund_sponsor_user_id: uuid.UUID | None,
) -> None:
    """Пополнить фонд может админ или его спонсор-владелец."""
    if role is UserRole.ADMIN:
        return
    if fund_sponsor_user_id is not None and actor_user_id == fund_sponsor_user_id:
        return
    raise BillingPermissionError("Пополнить фонд может админ или его спонсор")


def ensure_can_create_payout(role: UserRole) -> None:
    """Инициировать выплату (maker) может только администратор."""
    if role is not UserRole.ADMIN:
        raise BillingPermissionError("Начисление выплат доступно только admin")


def ensure_can_approve_payout(role: UserRole) -> None:
    """Подтверждать выплату (checker) может только администратор."""
    if role is not UserRole.ADMIN:
        raise BillingPermissionError("Подтверждение выплат доступно только admin")


def ensure_distinct_approver(
    *, created_by: uuid.UUID, approver_id: uuid.UUID
) -> None:
    """maker-checker: подтверждающий не может быть инициатором выплаты."""
    if created_by == approver_id:
        raise SelfApprovalError(
            "Подтверждающий выплату должен отличаться от инициатора (maker-checker)"
        )
