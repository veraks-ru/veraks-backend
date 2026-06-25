"""Юнит-тесты политик RBAC и разделения обязанностей resolutions."""

from __future__ import annotations

import uuid

import pytest

from app.modules.identity.domain.entities import UserRole
from app.modules.resolutions.domain.errors import (
    ResolutionPermissionError,
    SelfDisputeDecisionError,
)
from app.modules.resolutions.domain.policies import (
    ensure_can_decide_dispute,
    ensure_can_raise_dispute,
    ensure_can_resolve,
    ensure_not_self_decision,
)


@pytest.mark.parametrize(
    "role", [UserRole.EDITOR, UserRole.ARBITER, UserRole.ADMIN]
)
def test_resolve_allowed_for_staff(role: UserRole) -> None:
    ensure_can_resolve(role)  # не поднимает


def test_resolve_forbidden_for_user() -> None:
    with pytest.raises(ResolutionPermissionError):
        ensure_can_resolve(UserRole.USER)


@pytest.mark.parametrize(
    "role", [UserRole.USER, UserRole.EDITOR, UserRole.ARBITER, UserRole.ADMIN]
)
def test_raise_dispute_allowed_for_any_role(role: UserRole) -> None:
    ensure_can_raise_dispute(role)  # содержательный фильтр — участие, не роль


@pytest.mark.parametrize("role", [UserRole.ARBITER, UserRole.ADMIN])
def test_decide_allowed_for_arbiter_admin(role: UserRole) -> None:
    ensure_can_decide_dispute(role)


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.EDITOR])
def test_decide_forbidden_for_non_arbiter(role: UserRole) -> None:
    with pytest.raises(ResolutionPermissionError):
        ensure_can_decide_dispute(role)


def test_cannot_decide_own_dispute() -> None:
    same = uuid.uuid4()
    with pytest.raises(SelfDisputeDecisionError):
        ensure_not_self_decision(decided_by=same, raised_by=same)


def test_can_decide_others_dispute() -> None:
    ensure_not_self_decision(decided_by=uuid.uuid4(), raised_by=uuid.uuid4())
