"""Проводка выручки ``b2b_invoice`` при выдаче ключа (шов к домену billing).

Best-effort: если счета операционной кассы отсутствуют (нестандартная БД),
проводка пропускается — выдача ключа не должна падать из-за учёта.
"""

from __future__ import annotations

import uuid

from app.modules.billing.adapters.clock import SystemClock
from app.modules.billing.domain import chart
from app.modules.billing.domain.ledger import (
    EntryDirection,
    LedgerTransaction,
    PostingLeg,
    TransactionKind,
)
from app.modules.billing.ports.repositories import LedgerRepository


class BillingRevenueRecorder:
    def __init__(self, *, ledger: LedgerRepository) -> None:
        self._ledger = ledger
        self._clock = SystemClock()

    async def record_key_issued(
        self, *, actor_user_id: uuid.UUID, amount_kopecks: int, key_id: uuid.UUID
    ) -> None:
        cash = await self._ledger.get_account_by_code(chart.OPS_CASH_YOOKASSA)
        revenue = await self._ledger.get_account_by_code(chart.OPS_REVENUE_B2B)
        if cash is None or revenue is None:
            return
        transaction = LedgerTransaction.post(
            kind=TransactionKind.B2B_INVOICE,
            legs=(
                PostingLeg(cash, EntryDirection.DEBIT, amount_kopecks),
                PostingLeg(revenue, EntryDirection.CREDIT, amount_kopecks),
            ),
            external_ref=f"apikey:{key_id}",
            description="Выдача B2B-ключа",
            now=self._clock.now(),
        )
        await self._ledger.add_transaction(transaction)
