"""Возврат платежа ТБанк: провайдер + сторно OPERATIONS + статус refunded."""

import pytest

from app.modules.billing.domain import chart
from app.modules.billing.domain.entities import (
    PaymentProvider,
    PaymentStatus,
    SubscriptionPlan,
)
from app.modules.billing.domain.errors import InvalidPaymentError
from tests.billing.conftest import Stand


async def _paid(stand: Stand, user, *, provider, ref):
    sub, _ = await stand.start_subscription.execute(
        user_id=user.user_id, plan=SubscriptionPlan.MONTHLY
    )
    return await stand.record_payment.execute(
        provider=provider, provider_payment_id=ref,
        amount_kopecks=49_000, subscription_id=sub.id,
    )


async def test_refund_reverses_operations_and_marks_refunded(stand: Stand, user, admin):
    pay = await _paid(stand, user, provider=PaymentProvider.TBANK, ref="tb-9")

    refunded = await stand.refund_payment.execute(payment_id=pay.id, actor=admin)

    assert refunded.status is PaymentStatus.REFUNDED
    # приход и сторно взаимозачлись — операционный кэш ТБанк снова 0
    cash = await stand.ledger.get_account_by_code(chart.OPS_CASH_TBANK)
    assert await stand.ledger.balance(cash.id) == 0
    # провайдер вызван с чеком возврата
    assert len(stand.refund_gateway.calls) == 1
    assert stand.refund_gateway.calls[0]["receipt"] is not None
    assert "subscription.payment.refunded" in stand.audit.actions()


async def test_refund_twice_is_rejected(stand: Stand, user, admin):
    pay = await _paid(stand, user, provider=PaymentProvider.TBANK, ref="tb-10")
    await stand.refund_payment.execute(payment_id=pay.id, actor=admin)
    with pytest.raises(InvalidPaymentError):
        await stand.refund_payment.execute(payment_id=pay.id, actor=admin)


async def test_refund_non_tbank_rejected(stand: Stand, user, admin):
    pay = await _paid(stand, user, provider=PaymentProvider.YOOKASSA, ref="yk-1")
    with pytest.raises(InvalidPaymentError):
        await stand.refund_payment.execute(payment_id=pay.id, actor=admin)
