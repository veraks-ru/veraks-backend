"""E2E кабинета спонсора: self-serve анонс/пополнение своего фонда + приватность."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.adapters.clock import SystemClock as BillingClock
from app.modules.billing.adapters.repositories import (
    SqlAlchemyLedgerRepository,
    SqlAlchemyPayoutRepository,
    SqlAlchemyPrizeFundRepository,
)
from app.modules.billing.application.dto import Actor as BillingActor
from app.modules.billing.application.use_cases import (
    AnnouncePrizeFund,
    GetMySponsorFund,
    ListMySponsorFunds,
    RecordSponsorDeposit,
)
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    PrizeFundNotFoundError,
)
from app.modules.identity.domain.entities import UserRole
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from tests.e2e.helpers import add_user

pytestmark = pytest.mark.asyncio


async def test_sponsor_self_serve_fund_and_cabinet(session: AsyncSession) -> None:
    sponsor = await add_user(session, username="sponsor1")
    stranger = await add_user(session, username="stranger_s")
    await session.flush()

    clock = BillingClock()
    funds = SqlAlchemyPrizeFundRepository(session)
    ledger = SqlAlchemyLedgerRepository(session)
    payouts = SqlAlchemyPayoutRepository(session)
    audit = SqlAlchemyAuditTrail(session)
    actor = BillingActor(user_id=sponsor.id, role=UserRole.USER)

    # Спонсор (не админ) сам анонсирует свой фонд.
    fund = await AnnouncePrizeFund(
        funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(
        actor=actor,
        sponsor_name="ООО Ромашка",
        committed_kopecks=1_000_000,
        sponsor_user_id=sponsor.id,
    )
    assert fund.sponsor_user_id == sponsor.id

    # И сам пополняет его.
    await RecordSponsorDeposit(
        funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(actor=actor, fund_id=fund.id, amount_kopecks=600_000)

    # Кабинет: список моих фондов с доступным остатком.
    mine = await ListMySponsorFunds(funds=funds, ledger=ledger).execute(
        sponsor_user_id=sponsor.id
    )
    assert len(mine) == 1
    assert mine[0].available_kopecks == 600_000

    # Детали фонда + его выплаты (пока пусто).
    detail = await GetMySponsorFund(
        funds=funds, ledger=ledger, payouts=payouts
    ).execute(fund_id=fund.id, sponsor_user_id=sponsor.id)
    assert detail.available_kopecks == 600_000
    assert detail.payouts == []

    # Чужой фонд посторонний не видит (404-семантика).
    with pytest.raises(PrizeFundNotFoundError):
        await GetMySponsorFund(
            funds=funds, ledger=ledger, payouts=payouts
        ).execute(fund_id=fund.id, sponsor_user_id=stranger.id)

    # Посторонний не может пополнить чужой фонд.
    with pytest.raises(BillingPermissionError):
        await RecordSponsorDeposit(
            funds=funds, ledger=ledger, audit=audit, clock=clock
        ).execute(
            actor=BillingActor(user_id=stranger.id, role=UserRole.USER),
            fund_id=fund.id,
            amount_kopecks=100,
        )
    await session.commit()


async def test_non_sponsor_cannot_announce_foreign_fund(
    session: AsyncSession,
) -> None:
    a = await add_user(session, username="usr_a")
    b = await add_user(session, username="usr_b")
    await session.flush()
    # Обычный пользователь не может завести фонд «на чужое имя».
    with pytest.raises(BillingPermissionError):
        await AnnouncePrizeFund(
            funds=SqlAlchemyPrizeFundRepository(session),
            ledger=SqlAlchemyLedgerRepository(session),
            audit=SqlAlchemyAuditTrail(session),
            clock=BillingClock(),
        ).execute(
            actor=BillingActor(user_id=a.id, role=UserRole.USER),
            sponsor_name="Чужой",
            committed_kopecks=1,
            sponsor_user_id=b.id,
        )
