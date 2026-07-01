"""E2E призовой кассы (maker-checker выплаты) против реального Postgres.

Двойной контроль: инициатор (maker) создаёт выплату, подтверждает — другой
админ (checker). Самоподтверждение запрещено политикой. Проводки уходят в
append-only ledger; проверяем статусы и балансы счетов PRIZE.
"""

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
    ApprovePayout,
    CreatePayout,
    RecordSponsorDeposit,
)
from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import PayoutStatus
from app.modules.billing.domain.errors import SelfApprovalError
from app.modules.identity.domain.entities import UserRole
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from tests.e2e.helpers import add_user

pytestmark = pytest.mark.asyncio


async def test_maker_checker_payout_flow(session: AsyncSession) -> None:
    maker = await add_user(session, username="maker1", role=UserRole.ADMIN)
    checker = await add_user(session, username="checker1", role=UserRole.ADMIN)
    winner = await add_user(session, username="winner1")
    await session.flush()

    clock = BillingClock()
    ledger = SqlAlchemyLedgerRepository(session)
    funds = SqlAlchemyPrizeFundRepository(session)
    payouts = SqlAlchemyPayoutRepository(session)
    audit = SqlAlchemyAuditTrail(session)
    maker_actor = BillingActor(user_id=maker.id, role=UserRole.ADMIN)
    checker_actor = BillingActor(user_id=checker.id, role=UserRole.ADMIN)

    # Завести фонд и внести спонсорские деньги.
    fund = await AnnouncePrizeFund(
        funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(
        actor=maker_actor,
        sponsor_name="Спонсор X",
        committed_kopecks=1_000_000,
    )
    await RecordSponsorDeposit(
        funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(actor=maker_actor, fund_id=fund.id, amount_kopecks=1_000_000)

    # maker: инициировать выплату победителю (без проводки, статус PENDING).
    payout = await CreatePayout(
        payouts=payouts, funds=funds, audit=audit, clock=clock
    ).execute(
        actor=maker_actor,
        user_id=winner.id,
        prize_fund_id=fund.id,
        amount_kopecks=400_000,
        tax_withheld_kopecks=60_000,
    )
    assert payout.status is PayoutStatus.PENDING

    # Самоподтверждение запрещено (двойной контроль).
    with pytest.raises(SelfApprovalError):
        await ApprovePayout(
            payouts=payouts, funds=funds, ledger=ledger, audit=audit, clock=clock
        ).execute(actor=maker_actor, payout_id=payout.id)

    # checker (другой админ) подтверждает — проводка уходит в PRIZE.
    approved = await ApprovePayout(
        payouts=payouts, funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(actor=checker_actor, payout_id=payout.id)
    assert approved.status is PayoutStatus.APPROVED

    # Баланс фонда уменьшился на брутто (400k + 60k налога); к выплате начислено.
    fund_acc = await ledger.get_account_by_code(
        chart.prize_fund_account_code(fund.id)
    )
    payable = await ledger.get_account_by_code(chart.PRIZE_PAYABLE_WINNERS)
    assert fund_acc is not None and payable is not None
    # balance = debit − credit. Фонд держит кредитовое сальдо: было −1 000 000
    # (внесение), стало −540 000 после дебета брутто 460 000.
    assert await ledger.balance(fund_acc.id) == -(1_000_000 - 460_000)
    # К выплате победителям — кредит на нетто 400 000.
    assert await ledger.balance(payable.id) == -400_000
    await session.commit()
