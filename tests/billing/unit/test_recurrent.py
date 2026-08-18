"""Юнит-тесты автопродления подписки (через порты-фейки).

Покрывают: сохранение токена из уведомления о родительском платеже, продление
периода от его конца, отказ банка и предел попыток, отключение автопродления.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.modules.billing.application.dto import Actor
from app.modules.billing.application.use_cases import (
    ChargeRenewal,
    ListRenewableSubscriptions,
    SetAutoRenew,
)
from app.modules.billing.domain.entities import (
    PaymentProvider,
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.modules.billing.domain.errors import (
    BillingPermissionError,
    PaymentGatewayError,
    RecurrentNotEnabledError,
)
from app.modules.identity.domain.entities import UserRole
from tests.billing.conftest import FIXED_NOW
from tests.billing.fakes import FakeCheckoutGateway


def _subscription(**over) -> Subscription:
    base = {
        "user_id": uuid.uuid4(),
        "plan": SubscriptionPlan.MONTHLY,
        "price_kopecks": 99_000,
        "provider": PaymentProvider.TBANK,
        "status": SubscriptionStatus.ACTIVE,
        "current_period_end": FIXED_NOW + timedelta(hours=2),
        "rebill_id": "rebill-1",
        "auto_renew": True,
    }
    base.update(over)
    return Subscription(**base)  # type: ignore[arg-type]


async def test_parent_payment_saves_token_and_enables_renew(stand) -> None:
    """Токен из уведомления о первом платеже включает автопродление."""
    subscription, _ = await stand.start_subscription.execute(
        user_id=uuid.uuid4(), plan=SubscriptionPlan.MONTHLY
    )
    await stand.record_payment.execute(
        provider=PaymentProvider.TBANK,
        provider_payment_id="pay-1",
        amount_kopecks=subscription.price_kopecks,
        subscription_id=subscription.id,
        rebill_id="rebill-42",
    )

    saved = await stand.subscriptions.get_by_id(subscription.id)
    assert saved is not None
    assert saved.rebill_id == "rebill-42"
    assert saved.auto_renew is True
    assert saved.status is SubscriptionStatus.ACTIVE


async def test_checkout_asks_provider_for_recurrent(stand) -> None:
    """При оформлении провайдеру передаётся CustomerKey — иначе токена не будет."""
    user_id = uuid.uuid4()
    await stand.start_subscription.execute(
        user_id=user_id, plan=SubscriptionPlan.MONTHLY
    )
    assert stand.checkout.customer_keys == [str(user_id)]


async def test_renewal_extends_from_period_end(stand) -> None:
    """Списание идёт заранее, поэтому период считается от его конца."""
    subscription, _ = await stand.start_subscription.execute(
        user_id=uuid.uuid4(), plan=SubscriptionPlan.MONTHLY
    )
    await stand.record_payment.execute(
        provider=PaymentProvider.TBANK,
        provider_payment_id="pay-1",
        amount_kopecks=subscription.price_kopecks,
        subscription_id=subscription.id,
        rebill_id="rebill-1",
    )
    first = await stand.subscriptions.get_by_id(subscription.id)
    assert first is not None and first.current_period_end is not None
    first_end = first.current_period_end

    # Продление до истечения периода: оплаченные дни не должны сгорать.
    await stand.record_payment.execute(
        provider=PaymentProvider.TBANK,
        provider_payment_id="pay-2",
        amount_kopecks=subscription.price_kopecks,
        subscription_id=subscription.id,
    )
    second = await stand.subscriptions.get_by_id(subscription.id)
    assert second is not None and second.current_period_end is not None
    assert second.current_period_end > first_end


async def test_charge_uses_saved_token(subscriptions_repo, audit) -> None:
    """Списание уходит провайдеру с сохранённым токеном и ценой тарифа."""
    subscription = await subscriptions_repo.add(_subscription())
    gateway = FakeCheckoutGateway()

    payment_id = await ChargeRenewal(
        subscriptions=subscriptions_repo, checkout=gateway, audit=audit
    ).execute(subscription_id=subscription.id)

    assert payment_id is not None
    assert gateway.charges == [
        {
            "subscription_id": subscription.id,
            "amount_kopecks": 99_000,
            "rebill_id": "rebill-1",
            "customer_key": str(subscription.user_id),
        }
    ]


async def test_declined_charge_counts_attempt(subscriptions_repo, audit) -> None:
    """Отказ банка не гасит подписку сразу — попытки считаются."""
    subscription = await subscriptions_repo.add(_subscription())
    uc = ChargeRenewal(
        subscriptions=subscriptions_repo,
        checkout=FakeCheckoutGateway(fail_charge=True),
        audit=audit,
        max_attempts=3,
    )

    with pytest.raises(PaymentGatewayError):
        await uc.execute(subscription_id=subscription.id)

    after = await subscriptions_repo.get_by_id(subscription.id)
    assert after is not None
    assert after.renewal_attempts == 1
    assert after.auto_renew is True
    assert after.status is SubscriptionStatus.PAST_DUE


async def test_charging_stops_after_attempt_limit(subscriptions_repo, audit) -> None:
    """После лимита отказов карту больше не дёргаем."""
    subscription = await subscriptions_repo.add(_subscription(renewal_attempts=2))
    uc = ChargeRenewal(
        subscriptions=subscriptions_repo,
        checkout=FakeCheckoutGateway(fail_charge=True),
        audit=audit,
        max_attempts=3,
    )

    with pytest.raises(PaymentGatewayError):
        await uc.execute(subscription_id=subscription.id)

    after = await subscriptions_repo.get_by_id(subscription.id)
    assert after is not None
    assert after.auto_renew is False


async def test_cancel_stops_auto_renew(stand, user) -> None:
    """Отменённую подписку списывать нельзя ни при каких условиях."""
    subscription, _ = await stand.start_subscription.execute(
        user_id=user.user_id, plan=SubscriptionPlan.MONTHLY
    )
    await stand.record_payment.execute(
        provider=PaymentProvider.TBANK,
        provider_payment_id="pay-1",
        amount_kopecks=subscription.price_kopecks,
        subscription_id=subscription.id,
        rebill_id="rebill-1",
    )

    await stand.cancel_subscription.execute(
        subscription_id=subscription.id, actor=user
    )

    after = await stand.subscriptions.get_by_id(subscription.id)
    assert after is not None
    assert after.auto_renew is False


async def test_user_can_switch_auto_renew_off_and_on(subscriptions_repo, audit) -> None:
    """Отключение стирает способ оплаты, поэтому включить обратно нельзя."""
    subscription = await subscriptions_repo.add(_subscription())
    owner = Actor(user_id=subscription.user_id, role=UserRole.USER)
    uc = SetAutoRenew(subscriptions=subscriptions_repo, audit=audit)

    off = await uc.execute(
        subscription_id=subscription.id, actor=owner, enabled=False
    )
    assert off.auto_renew is False
    assert off.rebill_id is None

    with pytest.raises(RecurrentNotEnabledError):
        await uc.execute(subscription_id=subscription.id, actor=owner, enabled=True)


async def test_auto_renew_is_owner_only(subscriptions_repo, audit) -> None:
    subscription = await subscriptions_repo.add(_subscription())
    stranger = Actor(user_id=uuid.uuid4(), role=UserRole.USER)

    with pytest.raises(BillingPermissionError):
        await SetAutoRenew(subscriptions=subscriptions_repo, audit=audit).execute(
            subscription_id=subscription.id, actor=stranger, enabled=False
        )


async def test_only_due_subscriptions_are_selected(subscriptions_repo) -> None:
    """В работу берутся лишь те, у кого период на исходе и есть чем платить."""
    due = await subscriptions_repo.add(_subscription())
    far = await subscriptions_repo.add(
        _subscription(current_period_end=FIXED_NOW + timedelta(days=20))
    )
    manual = await subscriptions_repo.add(_subscription(auto_renew=False))
    exhausted = await subscriptions_repo.add(_subscription(renewal_attempts=3))

    ids = await ListRenewableSubscriptions(subscriptions=subscriptions_repo).execute(
        now=FIXED_NOW
    )

    assert due.id in ids
    assert far.id not in ids
    assert manual.id not in ids
    assert exhausted.id not in ids
