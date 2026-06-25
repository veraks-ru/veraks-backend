"""Юнит-тесты доменных инвариантов журнала (двойная запись, две кассы).

Это ядро задачи: проводка целиком в одной кассе, баланс дебет=кредит,
положительные суммы. Перетекание между кассами должно быть невозможно.
"""

from __future__ import annotations

import pytest

from app.modules.billing.domain.errors import (
    CrossLedgerEntryError,
    DegenerateTransactionError,
    NonPositiveAmountError,
    UnbalancedTransactionError,
)
from app.modules.billing.domain.ledger import (
    EntryDirection,
    LedgerAccount,
    LedgerTransaction,
    LedgerType,
    PostingLeg,
    TransactionKind,
    ledger_of_kind,
)


def _ops_account(code: str) -> LedgerAccount:
    return LedgerAccount(ledger_type=LedgerType.OPERATIONS, account_code=code, title=code)


def _prize_account(code: str) -> LedgerAccount:
    return LedgerAccount(ledger_type=LedgerType.PRIZE, account_code=code, title=code)


def test_balanced_single_till_transaction_posts() -> None:
    cash = _ops_account("ops:cash:yookassa")
    revenue = _ops_account("ops:revenue:subscriptions")

    txn = LedgerTransaction.post(
        kind=TransactionKind.SUBSCRIPTION_PAYMENT,
        legs=(
            PostingLeg(cash, EntryDirection.DEBIT, 49_000),
            PostingLeg(revenue, EntryDirection.CREDIT, 49_000),
        ),
    )

    assert txn.ledger_type is LedgerType.OPERATIONS
    assert txn.total() == 49_000
    assert len(txn.entries) == 2


def test_unbalanced_transaction_rejected() -> None:
    cash = _ops_account("ops:cash:yookassa")
    revenue = _ops_account("ops:revenue:subscriptions")

    with pytest.raises(UnbalancedTransactionError):
        LedgerTransaction.post(
            kind=TransactionKind.SUBSCRIPTION_PAYMENT,
            legs=(
                PostingLeg(cash, EntryDirection.DEBIT, 49_000),
                PostingLeg(revenue, EntryDirection.CREDIT, 48_000),
            ),
        )


def test_cross_ledger_entry_rejected() -> None:
    """Нога на счёт чужой кассы запрещена — деньги не перетекают между кассами."""
    ops_cash = _ops_account("ops:cash:yookassa")
    prize_fund = _prize_account("prize:fund:x")

    with pytest.raises(CrossLedgerEntryError):
        LedgerTransaction.post(
            kind=TransactionKind.SUBSCRIPTION_PAYMENT,  # OPERATIONS
            legs=(
                PostingLeg(ops_cash, EntryDirection.DEBIT, 1_000),
                PostingLeg(prize_fund, EntryDirection.CREDIT, 1_000),  # PRIZE!
            ),
        )


def test_kind_binds_to_till() -> None:
    """Вид призовой проводки нельзя провести по операционным счетам."""
    ops_a = _ops_account("ops:cash:yookassa")
    ops_b = _ops_account("ops:revenue:subscriptions")

    assert ledger_of_kind(TransactionKind.PRIZE_PAYOUT) is LedgerType.PRIZE
    with pytest.raises(CrossLedgerEntryError):
        LedgerTransaction.post(
            kind=TransactionKind.PRIZE_PAYOUT,  # PRIZE
            legs=(
                PostingLeg(ops_a, EntryDirection.DEBIT, 1_000),
                PostingLeg(ops_b, EntryDirection.CREDIT, 1_000),
            ),
        )


def test_degenerate_transaction_rejected() -> None:
    cash = _ops_account("ops:cash:yookassa")
    with pytest.raises(DegenerateTransactionError):
        LedgerTransaction.post(
            kind=TransactionKind.SUBSCRIPTION_PAYMENT,
            legs=(PostingLeg(cash, EntryDirection.DEBIT, 1_000),),
        )


def test_non_positive_amount_rejected() -> None:
    cash = _ops_account("ops:cash:yookassa")
    revenue = _ops_account("ops:revenue:subscriptions")
    with pytest.raises(NonPositiveAmountError):
        LedgerTransaction.post(
            kind=TransactionKind.SUBSCRIPTION_PAYMENT,
            legs=(
                PostingLeg(cash, EntryDirection.DEBIT, 0),
                PostingLeg(revenue, EntryDirection.CREDIT, 0),
            ),
        )


def test_three_leg_payout_balances() -> None:
    """Призовая выплата: брутто = нетто + НДФЛ, баланс сходится."""
    fund = _prize_account("prize:fund:x")
    payable = _prize_account("prize:payable:winners")
    tax = _prize_account("prize:tax:withheld")

    txn = LedgerTransaction.post(
        kind=TransactionKind.PRIZE_PAYOUT,
        legs=(
            PostingLeg(fund, EntryDirection.DEBIT, 10_000),
            PostingLeg(payable, EntryDirection.CREDIT, 8_700),
            PostingLeg(tax, EntryDirection.CREDIT, 1_300),
        ),
    )

    assert txn.ledger_type is LedgerType.PRIZE
    assert txn.total() == 10_000
