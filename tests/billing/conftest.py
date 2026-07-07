"""Сборка стенда billing: фейки портов + готовые use-cases на общих часах."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from app.modules.billing.application.dto import Actor
from app.modules.billing.application.use_cases import (
    AnnouncePrizeFund,
    ApprovePayout,
    CancelSubscription,
    CreatePayout,
    GetPrizeFund,
    RecordSponsorDeposit,
    RecordSubscriptionPayment,
    RefundSubscriptionPayment,
    StartSubscription,
)
from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import SubscriptionPlan
from app.modules.billing.domain.ledger import LedgerAccount, LedgerType
from app.modules.identity.domain.entities import UserRole
from tests.billing.fakes import (
    FakeAuditTrail,
    FakeCheckoutGateway,
    FakeClock,
    FakeRefundGateway,
    InMemoryLedgerRepository,
    InMemoryPaymentRepository,
    InMemoryPayoutRepository,
    InMemoryPrizeFundRepository,
    InMemorySubscriptionRepository,
)

FIXED_NOW = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
PLAN_PRICES = {
    SubscriptionPlan.MONTHLY: 49_000,
    SubscriptionPlan.ANNUAL: 490_000,
}

# Стандартный план счетов, который засевает миграция 0010.
_SEED_ACCOUNTS = [
    (LedgerType.OPERATIONS, chart.OPS_CASH_YOOKASSA, "Операционный кэш"),
    (LedgerType.OPERATIONS, chart.OPS_CASH_TBANK, "Операционный кэш ТБанк"),
    (LedgerType.OPERATIONS, chart.OPS_REVENUE_SUBSCRIPTIONS, "Выручка: подписки"),
    (LedgerType.PRIZE, chart.PRIZE_CASH_SPONSOR, "Призовой кэш спонсора"),
    (LedgerType.PRIZE, chart.PRIZE_PAYABLE_WINNERS, "К выплате победителям"),
    (LedgerType.PRIZE, chart.PRIZE_TAX_WITHHELD, "Удержанный НДФЛ"),
]


@dataclass
class Stand:
    """Стенд: фейки портов и готовые use-cases на общем времени."""

    clock: FakeClock
    ledger: InMemoryLedgerRepository
    subscriptions: InMemorySubscriptionRepository
    payments: InMemoryPaymentRepository
    funds: InMemoryPrizeFundRepository
    payouts: InMemoryPayoutRepository
    audit: FakeAuditTrail
    start_subscription: StartSubscription
    cancel_subscription: CancelSubscription
    record_payment: RecordSubscriptionPayment
    announce_fund: AnnouncePrizeFund
    record_deposit: RecordSponsorDeposit
    get_fund: GetPrizeFund
    create_payout: CreatePayout
    approve_payout: ApprovePayout
    refund_gateway: FakeRefundGateway
    refund_payment: RefundSubscriptionPayment


@pytest.fixture
def stand() -> Stand:
    """Стенд с засеянным планом счетов и собранными use-cases."""
    clock = FakeClock(FIXED_NOW)
    ledger = InMemoryLedgerRepository()
    for ltype, code, title in _SEED_ACCOUNTS:
        ledger.seed_account(
            LedgerAccount(ledger_type=ltype, account_code=code, title=title)
        )
    subscriptions = InMemorySubscriptionRepository()
    payments = InMemoryPaymentRepository()
    funds = InMemoryPrizeFundRepository()
    payouts = InMemoryPayoutRepository()
    audit = FakeAuditTrail()
    checkout = FakeCheckoutGateway()
    refund_gateway = FakeRefundGateway()

    return Stand(
        clock=clock,
        ledger=ledger,
        subscriptions=subscriptions,
        payments=payments,
        funds=funds,
        payouts=payouts,
        audit=audit,
        start_subscription=StartSubscription(
            subscriptions=subscriptions,
            checkout=checkout,
            audit=audit,
            clock=clock,
            plan_prices=PLAN_PRICES,
        ),
        cancel_subscription=CancelSubscription(
            subscriptions=subscriptions, audit=audit, clock=clock
        ),
        record_payment=RecordSubscriptionPayment(
            payments=payments,
            subscriptions=subscriptions,
            ledger=ledger,
            audit=audit,
            clock=clock,
        ),
        announce_fund=AnnouncePrizeFund(
            funds=funds, ledger=ledger, audit=audit, clock=clock
        ),
        record_deposit=RecordSponsorDeposit(
            funds=funds, ledger=ledger, audit=audit, clock=clock
        ),
        get_fund=GetPrizeFund(funds=funds, ledger=ledger),
        create_payout=CreatePayout(
            payouts=payouts, funds=funds, audit=audit, clock=clock
        ),
        approve_payout=ApprovePayout(
            payouts=payouts, funds=funds, ledger=ledger, audit=audit, clock=clock
        ),
        refund_gateway=refund_gateway,
        refund_payment=RefundSubscriptionPayment(
            payments=payments,
            ledger=ledger,
            gateway=refund_gateway,
            audit=audit,
            clock=clock,
            taxation="usn_income",
        ),
    )


@pytest.fixture
def admin() -> Actor:
    """Администратор (maker)."""
    return Actor(user_id=uuid.uuid4(), role=UserRole.ADMIN)


@pytest.fixture
def admin2() -> Actor:
    """Второй администратор (checker) — для maker-checker."""
    return Actor(user_id=uuid.uuid4(), role=UserRole.ADMIN)


@pytest.fixture
def user() -> Actor:
    """Обычный пользователь."""
    return Actor(user_id=uuid.uuid4(), role=UserRole.USER)
