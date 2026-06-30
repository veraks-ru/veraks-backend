"""Адаптер подписочного гейта поверх таблицы подписок billing.

Интеграционный шов predictions → billing (как ``user_gateway`` читает
``UserORM`` домена identity). Активной считается подписка со статусом
``active`` и непросроченным ``current_period_end``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.adapters.orm import SubscriptionORM
from app.modules.billing.domain.entities import SubscriptionStatus


class SqlAlchemySubscriptionGate:
    """Проверка активной подписки прямым запросом к ``subscriptions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_active_subscription(
        self, user_id: uuid.UUID, now: datetime
    ) -> bool:
        stmt = (
            select(SubscriptionORM.id)
            .where(
                SubscriptionORM.user_id == user_id,
                SubscriptionORM.status == SubscriptionStatus.ACTIVE,
                SubscriptionORM.current_period_end.is_not(None),
                SubscriptionORM.current_period_end > now,
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).first() is not None
