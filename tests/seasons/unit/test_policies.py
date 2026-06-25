"""Юнит-тесты RBAC-политик сезонов (разделение обязанностей)."""

from __future__ import annotations

import pytest

from app.modules.identity.domain.entities import UserRole
from app.modules.seasons.domain.errors import SeasonPermissionError
from app.modules.seasons.domain.policies import (
    ensure_can_manage_seasons,
    ensure_can_transition,
)


@pytest.mark.parametrize("role", [UserRole.EDITOR, UserRole.ADMIN])
def test_editor_and_admin_can_manage_seasons(role: UserRole) -> None:
    ensure_can_manage_seasons(role)  # не бросает


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.ARBITER])
def test_others_cannot_manage_seasons(role: UserRole) -> None:
    with pytest.raises(SeasonPermissionError):
        ensure_can_manage_seasons(role)


def test_only_admin_can_transition() -> None:
    ensure_can_transition(UserRole.ADMIN)  # не бросает


@pytest.mark.parametrize("role", [UserRole.USER, UserRole.EDITOR, UserRole.ARBITER])
def test_non_admin_cannot_transition(role: UserRole) -> None:
    with pytest.raises(SeasonPermissionError):
        ensure_can_transition(role)
