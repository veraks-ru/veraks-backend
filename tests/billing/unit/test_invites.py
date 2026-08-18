"""Юнит-тесты пригласительного доступа (через порты-фейки).

Покрывают: право выдавать приглашения, срок доступа от момента активации,
одноразовость ссылки и отзыв.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.billing.application.dto import Actor
from app.modules.billing.application.use_cases import (
    CreateInvite,
    ListInvites,
    RedeemInvite,
    RevokeInvite,
)
from app.modules.billing.domain.entities import AccessGrant, Invite
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    InvalidInviteError,
    InviteAlreadyRedeemedError,
    InviteNotFoundError,
    InviteRevokedError,
)
from app.modules.identity.domain.entities import UserRole
from tests.billing.conftest import FIXED_NOW
from tests.billing.fakes import FakeAuditTrail, FakeClock


class InMemoryInviteRepository:
    """Приглашения в памяти."""

    def __init__(self) -> None:
        self._by_id: dict[uuid.UUID, Invite] = {}

    async def add(self, invite: Invite) -> Invite:
        self._by_id[invite.id] = invite
        return invite

    async def get_by_code(self, code: str) -> Invite | None:
        return next(
            (inv for inv in self._by_id.values() if inv.code == code), None
        )

    async def get_by_id(self, invite_id: uuid.UUID) -> Invite | None:
        return self._by_id.get(invite_id)

    async def update(self, invite: Invite) -> Invite:
        self._by_id[invite.id] = invite
        return invite

    async def list_recent(self, limit: int = 100) -> list[Invite]:
        rows = sorted(
            self._by_id.values(), key=lambda inv: inv.created_at, reverse=True
        )
        return rows[:limit]


class InMemoryAccessGrantRepository:
    """Выданный доступ в памяти."""

    def __init__(self) -> None:
        self.rows: list[AccessGrant] = []

    async def add(self, grant: AccessGrant) -> AccessGrant:
        self.rows.append(grant)
        return grant

    async def list_by_user(self, user_id: uuid.UUID) -> list[AccessGrant]:
        return [g for g in self.rows if g.user_id == user_id]


@pytest.fixture
def invites() -> InMemoryInviteRepository:
    return InMemoryInviteRepository()


@pytest.fixture
def grants() -> InMemoryAccessGrantRepository:
    return InMemoryAccessGrantRepository()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(FIXED_NOW)


@pytest.fixture
def audit() -> FakeAuditTrail:
    return FakeAuditTrail()


@pytest.fixture
def create(invites, audit, clock) -> CreateInvite:
    return CreateInvite(invites=invites, audit=audit, clock=clock)


@pytest.fixture
def redeem(invites, grants, audit, clock) -> RedeemInvite:
    return RedeemInvite(invites=invites, grants=grants, audit=audit, clock=clock)


async def test_unlimited_invite_grants_access_without_expiry(
    create, redeem, admin
) -> None:
    """Бессрочное приглашение даёт доступ, который не заканчивается."""
    invite = await create.execute(actor=admin, note="Канал в телеграме")
    newcomer = uuid.uuid4()

    grant = await redeem.execute(user_id=newcomer, code=invite.code)

    assert grant.expires_at is None
    assert grant.is_active(FIXED_NOW + timedelta(days=365 * 10))


async def test_period_invite_counts_from_redemption(create, redeem, clock, admin) -> None:
    """Срок идёт с активации, а не с создания: ссылка могла долго лежать."""
    invite = await create.execute(actor=admin, duration_days=30)
    clock.move_to(FIXED_NOW + timedelta(days=100))  # ссылка пролежала три месяца

    grant = await redeem.execute(user_id=uuid.uuid4(), code=invite.code)

    assert grant.expires_at == FIXED_NOW + timedelta(days=130)
    assert grant.is_active(clock.now())
    assert not grant.is_active(clock.now() + timedelta(days=31))


async def test_invite_is_single_use(create, redeem, admin) -> None:
    """Второй человек по той же ссылке доступа не получает."""
    invite = await create.execute(actor=admin)
    await redeem.execute(user_id=uuid.uuid4(), code=invite.code)

    with pytest.raises(InviteAlreadyRedeemedError):
        await redeem.execute(user_id=uuid.uuid4(), code=invite.code)


async def test_second_redemption_by_same_user_is_idempotent(
    create, redeem, grants, admin
) -> None:
    """Повторный переход по своей же ссылке возвращает выданный доступ."""
    invite = await create.execute(actor=admin)
    someone = uuid.uuid4()

    first = await redeem.execute(user_id=someone, code=invite.code)
    second = await redeem.execute(user_id=someone, code=invite.code)

    assert first.id == second.id
    assert len(grants.rows) == 1


async def test_revoked_invite_cannot_be_redeemed(
    create, redeem, invites, audit, clock, admin
) -> None:
    """Отозванная ссылка не работает."""
    invite = await create.execute(actor=admin)
    await RevokeInvite(invites=invites, audit=audit, clock=clock).execute(
        actor=admin, invite_id=invite.id
    )

    with pytest.raises(InviteRevokedError):
        await redeem.execute(user_id=uuid.uuid4(), code=invite.code)


async def test_used_invite_cannot_be_revoked(
    create, redeem, invites, audit, clock, admin
) -> None:
    """Использованную ссылку отзывать поздно — доступ уже выдан."""
    invite = await create.execute(actor=admin)
    await redeem.execute(user_id=uuid.uuid4(), code=invite.code)

    with pytest.raises(InviteAlreadyRedeemedError):
        await RevokeInvite(invites=invites, audit=audit, clock=clock).execute(
            actor=admin, invite_id=invite.id
        )


async def test_unknown_code_is_not_found(redeem) -> None:
    with pytest.raises(InviteNotFoundError):
        await redeem.execute(user_id=uuid.uuid4(), code="NbwcfCWJIIc")


async def test_only_admin_creates_invites(create, user) -> None:
    """Приглашение — отказ от выручки, поэтому не редакторское право."""
    with pytest.raises(BillingPermissionError):
        await create.execute(actor=user)

    editor = Actor(user_id=uuid.uuid4(), role=UserRole.EDITOR)
    with pytest.raises(BillingPermissionError):
        await create.execute(actor=editor)


async def test_non_positive_duration_rejected(create, admin) -> None:
    with pytest.raises(InvalidInviteError):
        await create.execute(actor=admin, duration_days=0)


async def test_listing_requires_admin(invites, create, admin, user) -> None:
    await create.execute(actor=admin, note="первое")
    uc = ListInvites(invites=invites)

    assert len(await uc.execute(actor=admin)) == 1
    with pytest.raises(BillingPermissionError):
        await uc.execute(actor=user)


async def test_codes_are_unique_and_short(create, admin) -> None:
    """Код короткий и разный у каждой ссылки."""
    first = await create.execute(actor=admin)
    second = await create.execute(actor=admin)

    assert len(first.code) == 11
    assert first.code != second.code
