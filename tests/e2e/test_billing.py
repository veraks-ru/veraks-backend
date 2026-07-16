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


# ── Реквизиты выплат Jump (СБП): шифрование, UNIQUE(user_id), enum jump ─────


async def test_payout_requisites_roundtrip_and_upsert(session: AsyncSession) -> None:
    """Реквизиты шифруются на записи и читаются обратно; upsert не плодит строк.

    Проверяется настоящий шов: bytea-колонки, UNIQUE(user_id) и Fernet-цикл
    encrypt→decrypt через реальный репозиторий.
    """
    from cryptography.fernet import Fernet
    from sqlalchemy import text

    from app.modules.billing.adapters.repositories import (
        SqlAlchemyPayoutRequisiteRepository,
    )
    from app.modules.billing.domain.entities import PayoutRequisites
    from app.modules.identity.adapters.security import FernetFieldEncryptor

    user = await add_user(session, username="req1")
    await session.flush()
    encryptor = FernetFieldEncryptor(Fernet.generate_key().decode())
    repo = SqlAlchemyPayoutRequisiteRepository(session, encryptor)

    created = await repo.upsert(
        PayoutRequisites(
            user_id=user.id,
            phone="8 (900) 123-45-67",
            sbp_bank_id="100000000004",
            last_name="Иванов",
            first_name="Пётр",
            middle_name="Сергеевич",
        )
    )
    fetched = await repo.get_by_user(user.id)
    assert fetched is not None
    assert fetched.phone == "+79001234567"
    assert fetched.last_name == "Иванов"

    # В БД ПДн не в открытом виде.
    row = (
        await session.execute(
            text(
                "SELECT sbp_phone_enc, last_name_enc FROM payout_requisites "
                "WHERE user_id = :uid"
            ),
            {"uid": str(user.id)},
        )
    ).one()
    assert b"+79001234567" not in bytes(row.sbp_phone_enc)
    assert "Иванов".encode() not in bytes(row.last_name_enc)

    # Upsert обновляет ту же строку (UNIQUE(user_id)).
    await repo.upsert(
        PayoutRequisites(
            user_id=user.id,
            phone="+79007654321",
            sbp_bank_id="100000000111",
            last_name="Иванов",
            first_name="Пётр",
            id=created.id,
        )
    )
    count = (
        await session.execute(
            text("SELECT count(*) FROM payout_requisites WHERE user_id = :uid"),
            {"uid": str(user.id)},
        )
    ).scalar_one()
    assert count == 1
    assert (await repo.get_by_user(user.id)).phone == "+79007654321"


async def test_payment_provider_enum_accepts_jump(session: AsyncSession) -> None:
    """Нативный PG-enum ``payment_provider`` принимает значение ``jump``."""
    from app.modules.billing.domain.entities import (
        PaymentProvider,
        PayoutStatus,
    )

    admin = await add_user(session, username="jadmin", role=UserRole.ADMIN)
    checker = await add_user(session, username="jchecker", role=UserRole.ADMIN)
    winner = await add_user(session, username="jwinner")
    await session.flush()

    clock = BillingClock()
    ledger = SqlAlchemyLedgerRepository(session)
    funds = SqlAlchemyPrizeFundRepository(session)
    payouts = SqlAlchemyPayoutRepository(session)
    audit = SqlAlchemyAuditTrail(session)
    admin_actor = BillingActor(user_id=admin.id, role=UserRole.ADMIN)

    fund = await AnnouncePrizeFund(
        funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(actor=admin_actor, sponsor_name="Спонсор J", committed_kopecks=50_000)
    await RecordSponsorDeposit(
        funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(actor=admin_actor, fund_id=fund.id, amount_kopecks=50_000)
    payout = await CreatePayout(
        payouts=payouts, funds=funds, audit=audit, clock=clock
    ).execute(
        actor=admin_actor,
        user_id=winner.id,
        prize_fund_id=fund.id,
        amount_kopecks=10_000,
    )
    await ApprovePayout(
        payouts=payouts, funds=funds, ledger=ledger, audit=audit, clock=clock
    ).execute(
        actor=BillingActor(user_id=checker.id, role=UserRole.ADMIN),
        payout_id=payout.id,
    )

    stored = await payouts.get_by_id(payout.id)
    assert stored is not None
    stored.mark_processing(provider=PaymentProvider.JUMP, provider_payout_id="15731787")
    saved = await payouts.update(stored)
    assert saved.provider is PaymentProvider.JUMP

    listed = await payouts.list_by_status(
        PayoutStatus.PROCESSING, provider=PaymentProvider.JUMP
    )
    assert [p.id for p in listed] == [payout.id]
