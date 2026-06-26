"""Юнит-тесты use-cases billing на стенде с фейками портов."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import (
    PaymentProvider,
    PaymentStatus,
    PayoutStatus,
    PrizeFundStatus,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    InsufficientPrizeFundError,
    PayoutAlreadyDecidedError,
    SelfApprovalError,
)
from tests.billing.conftest import FIXED_NOW, Stand


# ── Подписки / платежи (OPERATIONS) ───────────────────────────────────────


async def test_start_subscription_creates_incomplete_and_returns_url(
    stand: Stand, user
) -> None:
    sub, url = await stand.start_subscription.execute(
        user_id=user.user_id, plan=SubscriptionPlan.MONTHLY
    )

    assert sub.status is SubscriptionStatus.INCOMPLETE
    assert sub.price_kopecks == 49_000
    assert sub.provider_subscription_id is not None
    assert url.startswith("https://pay.example/")
    assert "subscription.started" in stand.audit.actions()


async def test_record_payment_posts_operations_and_activates(
    stand: Stand, user
) -> None:
    sub, _ = await stand.start_subscription.execute(
        user_id=user.user_id, plan=SubscriptionPlan.MONTHLY
    )

    payment = await stand.record_payment.execute(
        provider=PaymentProvider.YOOKASSA,
        provider_payment_id="pay-1",
        amount_kopecks=49_000,
        subscription_id=sub.id,
    )

    assert payment.status is PaymentStatus.SUCCEEDED
    assert payment.ledger_transaction_id is not None
    # деньги осели в операционной кассе
    cash = await stand.ledger.get_account_by_code(chart.OPS_CASH_YOOKASSA)
    revenue = await stand.ledger.get_account_by_code(chart.OPS_REVENUE_SUBSCRIPTIONS)
    assert await stand.ledger.balance(cash.id) == 49_000
    assert await stand.ledger.balance(revenue.id) == -49_000  # кредит
    # подписка активирована на период
    refreshed = await stand.subscriptions.get_by_id(sub.id)
    assert refreshed.status is SubscriptionStatus.ACTIVE
    assert refreshed.current_period_end == FIXED_NOW + timedelta(days=30)


async def test_record_payment_is_idempotent(stand: Stand, user) -> None:
    sub, _ = await stand.start_subscription.execute(
        user_id=user.user_id, plan=SubscriptionPlan.MONTHLY
    )
    first = await stand.record_payment.execute(
        provider=PaymentProvider.YOOKASSA,
        provider_payment_id="pay-dup",
        amount_kopecks=49_000,
        subscription_id=sub.id,
    )
    second = await stand.record_payment.execute(
        provider=PaymentProvider.YOOKASSA,
        provider_payment_id="pay-dup",
        amount_kopecks=49_000,
        subscription_id=sub.id,
    )

    assert first.id == second.id
    assert len(stand.payments.items) == 1
    assert len(stand.ledger.transactions) == 1  # вторая проводка не создана


# ── Призовой фонд (PRIZE) ─────────────────────────────────────────────────


async def test_announce_fund_creates_prize_account(stand: Stand, admin) -> None:
    fund = await stand.announce_fund.execute(
        actor=admin, sponsor_name="Acme", committed_kopecks=1_000_000
    )

    assert fund.status is PrizeFundStatus.ANNOUNCED
    account = await stand.ledger.get_account_by_code(
        chart.prize_fund_account_code(fund.id)
    )
    assert account is not None
    assert account.id == fund.ledger_account_id


async def test_announce_fund_requires_admin(stand: Stand, user) -> None:
    with pytest.raises(BillingPermissionError):
        await stand.announce_fund.execute(
            actor=user, sponsor_name="Acme", committed_kopecks=1
        )


async def test_sponsor_deposit_posts_prize_and_updates_fund(
    stand: Stand, admin
) -> None:
    fund = await stand.announce_fund.execute(
        actor=admin, sponsor_name="Acme", committed_kopecks=1_000_000
    )

    updated = await stand.record_deposit.execute(
        actor=admin, fund_id=fund.id, amount_kopecks=500_000
    )

    assert updated.status is PrizeFundStatus.FUNDED
    assert updated.deposited_kopecks == 500_000
    view = await stand.get_fund.execute(fund_id=fund.id)
    assert view.balance_kopecks == 500_000  # кредит на счёте фонда


# ── Выплаты призов (PRIZE, maker-checker) ─────────────────────────────────


async def _funded_fund(stand: Stand, admin, amount: int = 1_000_000):
    fund = await stand.announce_fund.execute(
        actor=admin, sponsor_name="Acme", committed_kopecks=amount
    )
    await stand.record_deposit.execute(
        actor=admin, fund_id=fund.id, amount_kopecks=amount
    )
    return fund


async def test_payout_maker_checker_happy_path(stand: Stand, admin, admin2) -> None:
    fund = await _funded_fund(stand, admin)

    payout = await stand.create_payout.execute(
        actor=admin,
        user_id=uuid.uuid4(),
        prize_fund_id=fund.id,
        amount_kopecks=8_700,
        tax_withheld_kopecks=1_300,
    )
    assert payout.status is PayoutStatus.PENDING

    approved = await stand.approve_payout.execute(actor=admin2, payout_id=payout.id)

    assert approved.status is PayoutStatus.APPROVED
    assert approved.approved_by == admin2.user_id
    assert approved.ledger_transaction_id is not None
    # фонд уменьшился на брутто (нетто + налог)
    view = await stand.get_fund.execute(fund_id=fund.id)
    assert view.balance_kopecks == 1_000_000 - 10_000
    # нетто и налог разнесены по призовым счетам
    payable = await stand.ledger.get_account_by_code(chart.PRIZE_PAYABLE_WINNERS)
    tax = await stand.ledger.get_account_by_code(chart.PRIZE_TAX_WITHHELD)
    assert await stand.ledger.balance(payable.id) == -8_700
    assert await stand.ledger.balance(tax.id) == -1_300
    assert "prize.payout.approved" in stand.audit.actions()


async def test_payout_self_approval_rejected(stand: Stand, admin) -> None:
    """maker-checker: тот же админ не может подтвердить свою выплату."""
    fund = await _funded_fund(stand, admin)
    payout = await stand.create_payout.execute(
        actor=admin, user_id=uuid.uuid4(), prize_fund_id=fund.id, amount_kopecks=1_000
    )

    with pytest.raises(SelfApprovalError):
        await stand.approve_payout.execute(actor=admin, payout_id=payout.id)
    # проводки не было
    assert all(t.kind.value != "prize_payout" for t in stand.ledger.transactions)


async def test_payout_double_approval_rejected(stand: Stand, admin, admin2) -> None:
    fund = await _funded_fund(stand, admin)
    payout = await stand.create_payout.execute(
        actor=admin, user_id=uuid.uuid4(), prize_fund_id=fund.id, amount_kopecks=1_000
    )
    await stand.approve_payout.execute(actor=admin2, payout_id=payout.id)

    with pytest.raises(PayoutAlreadyDecidedError):
        await stand.approve_payout.execute(actor=admin2, payout_id=payout.id)


async def test_payout_insufficient_fund_rejected(stand: Stand, admin, admin2) -> None:
    fund = await _funded_fund(stand, admin, amount=5_000)
    payout = await stand.create_payout.execute(
        actor=admin, user_id=uuid.uuid4(), prize_fund_id=fund.id, amount_kopecks=9_000
    )

    with pytest.raises(InsufficientPrizeFundError):
        await stand.approve_payout.execute(actor=admin2, payout_id=payout.id)


async def test_create_payout_requires_admin(stand: Stand, admin, user) -> None:
    fund = await _funded_fund(stand, admin)
    with pytest.raises(BillingPermissionError):
        await stand.create_payout.execute(
            actor=user,
            user_id=uuid.uuid4(),
            prize_fund_id=fund.id,
            amount_kopecks=1_000,
        )


# ── Сверка журнала (ReconcileLedger) ──────────────────────────────────────


async def test_reconcile_ledger_books_balanced(stand: Stand, admin, user) -> None:
    """После проводок обе кассы сходятся (дебеты == кредиты)."""
    from app.modules.billing.application.use_cases import ReconcileLedger
    from app.modules.billing.domain.ledger import LedgerType

    # OPERATIONS: приём платежа по подписке.
    sub, _ = await stand.start_subscription.execute(
        user_id=user.user_id, plan=SubscriptionPlan.MONTHLY
    )
    await stand.record_payment.execute(
        provider=PaymentProvider.YOOKASSA,
        provider_payment_id="pay-rec",
        amount_kopecks=49_000,
        subscription_id=sub.id,
    )
    # PRIZE: депозит спонсора.
    await _funded_fund(stand, admin, amount=1_000_000)

    reports = await ReconcileLedger(ledger=stand.ledger).execute()

    by_type = {r.ledger_type: r for r in reports}
    assert by_type[LedgerType.OPERATIONS].balanced is True
    assert by_type[LedgerType.OPERATIONS].total_debit_kopecks == 49_000
    assert by_type[LedgerType.PRIZE].balanced is True
    assert by_type[LedgerType.PRIZE].total_debit_kopecks == 1_000_000


async def test_reconcile_empty_ledger_is_balanced(stand: Stand) -> None:
    from app.modules.billing.application.use_cases import ReconcileLedger

    reports = await ReconcileLedger(ledger=stand.ledger).execute()
    assert all(r.balanced for r in reports)
    assert all(r.total_debit_kopecks == 0 for r in reports)
