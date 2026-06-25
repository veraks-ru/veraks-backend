"""Юнит-тесты политик billing (RBAC и maker-checker)."""

from __future__ import annotations

import uuid

import pytest

from app.modules.billing.domain.errors import (
    BillingPermissionError,
    SelfApprovalError,
)
from app.modules.billing.domain.policies import (
    ensure_can_approve_payout,
    ensure_can_create_payout,
    ensure_can_manage_prize_funds,
    ensure_distinct_approver,
)
from app.modules.identity.domain.entities import UserRole


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.EDITOR, UserRole.ARBITER])
def test_non_admin_cannot_manage_funds_or_payouts(role: UserRole) -> None:
    with pytest.raises(BillingPermissionError):
        ensure_can_manage_prize_funds(role)
    with pytest.raises(BillingPermissionError):
        ensure_can_create_payout(role)
    with pytest.raises(BillingPermissionError):
        ensure_can_approve_payout(role)


def test_admin_allowed() -> None:
    ensure_can_manage_prize_funds(UserRole.ADMIN)
    ensure_can_create_payout(UserRole.ADMIN)
    ensure_can_approve_payout(UserRole.ADMIN)


def test_self_approval_rejected() -> None:
    same = uuid.uuid4()
    with pytest.raises(SelfApprovalError):
        ensure_distinct_approver(created_by=same, approver_id=same)


def test_distinct_approver_allowed() -> None:
    ensure_distinct_approver(created_by=uuid.uuid4(), approver_id=uuid.uuid4())
