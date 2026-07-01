"""E2E подписочного гейта на голосовании против реального Postgres.

Без активной подписки ``PlacePrediction`` отдаёт 402-доменную ошибку; после
выдачи подписки (реальная запись в ``subscriptions``) — прогноз ставится.
Гейт бьётся в БД через ``SqlAlchemySubscriptionGate`` (не фейк).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.adapters.repositories import (
    SqlAlchemySubscriptionRepository,
)
from app.modules.billing.domain.entities import (
    PaymentProvider,
    Subscription,
    SubscriptionPlan,
)
from app.modules.events.adapters.repository import SqlAlchemyEventRepository
from app.modules.predictions.adapters.audit_trail import AuditTrailRecorder
from app.modules.predictions.adapters.event_gateway import EventRepositoryGateway
from app.modules.predictions.adapters.repository import (
    SqlAlchemyPredictionRepository,
)
from app.modules.predictions.adapters.subscription_gate import (
    SqlAlchemySubscriptionGate,
)
from app.modules.predictions.application.use_cases import PlacePrediction
from app.modules.predictions.domain.entities import ConfidenceGrade
from app.modules.identity.domain.entities import UserRole
from app.modules.predictions.domain.errors import (
    PredictionSubscriptionRequiredError,
)
from app.shared.audit.adapters.trail import SqlAlchemyAuditTrail
from tests.e2e.helpers import (
    OPENS_AT,
    FixedClock,
    add_active_season,
    add_category,
    add_open_event,
    add_user,
)

pytestmark = pytest.mark.asyncio

_WHEN = OPENS_AT + timedelta(days=1)  # внутри окна приёма


def _place_uc(session) -> PlacePrediction:  # noqa: ANN001
    return PlacePrediction(
        predictions=SqlAlchemyPredictionRepository(session),
        events=EventRepositoryGateway(SqlAlchemyEventRepository(session)),
        clock=FixedClock(_WHEN),
        audit=AuditTrailRecorder(SqlAlchemyAuditTrail(session)),
        subscriptions=SqlAlchemySubscriptionGate(session),
    )


async def _open_event(session, admin_id):  # noqa: ANN001
    category = await add_category(session)
    season = await add_active_season(session)
    await session.flush()
    return await add_open_event(
        session, category_id=category.id, created_by=admin_id, season_id=season.id
    )


async def test_place_rejected_without_subscription(session: AsyncSession) -> None:
    admin = await add_user(session, username="ed1", role=UserRole.ADMIN)
    voter = await add_user(session, username="poor1")
    event = await _open_event(session, admin.id)

    with pytest.raises(PredictionSubscriptionRequiredError):
        await _place_uc(session).execute(
            user_id=voter.id,
            event_id=event.id,
            grade=ConfidenceGrade.PROBABLY_YES,
        )


async def test_place_succeeds_with_active_subscription(
    session: AsyncSession,
) -> None:
    admin = await add_user(session, username="ed2", role=UserRole.ADMIN)
    voter = await add_user(session, username="paid1")
    event = await _open_event(session, admin.id)

    sub = Subscription(
        user_id=voter.id,
        plan=SubscriptionPlan.MONTHLY,
        price_kopecks=99000,
        provider=PaymentProvider.YOOKASSA,
    )
    sub.activate(period_start=_WHEN, period_end=_WHEN + timedelta(days=30))
    await SqlAlchemySubscriptionRepository(session).add(sub)
    await session.flush()

    result = await _place_uc(session).execute(
        user_id=voter.id,
        event_id=event.id,
        grade=ConfidenceGrade.PROBABLY_YES,
    )
    assert result.probability is not None
    stored = await SqlAlchemyPredictionRepository(session).get_for_user_event(
        voter.id, event.id
    )
    assert stored is not None
    await session.commit()
