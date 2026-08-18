"""Адаптер подписочного гейта поверх таблиц billing.

Интеграционный шов predictions → billing (как ``user_gateway`` читает
``UserORM`` домена identity). Голосовать можно при активной подписке
(статус ``active`` и непросроченный ``current_period_end``) либо при
действующем доступе по приглашению — это второй, неоплаченный путь к тому же
праву, поэтому проверять надо оба.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.billing.adapters.orm import AccessGrantORM, SubscriptionORM
from app.modules.billing.domain.entities import SubscriptionStatus


class SqlAlchemySubscriptionGate:
    """Проверка активной подписки прямым запросом к ``subscriptions``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def has_active_subscription(
        self, user_id: uuid.UUID, now: datetime
    ) -> bool:
        paid = (
            select(SubscriptionORM.id)
            .where(
                SubscriptionORM.user_id == user_id,
                SubscriptionORM.status == SubscriptionStatus.ACTIVE,
                SubscriptionORM.current_period_end.is_not(None),
                SubscriptionORM.current_period_end > now,
            )
            .limit(1)
        )
        if (await self._session.execute(paid)).first() is not None:
            return True

        # Доступ по приглашению: NULL в expires_at — бессрочный.
        granted = (
            select(AccessGrantORM.id)
            .where(
                AccessGrantORM.user_id == user_id,
                or_(
                    AccessGrantORM.expires_at.is_(None),
                    AccessGrantORM.expires_at > now,
                ),
            )
            .limit(1)
        )
        return (await self._session.execute(granted)).first() is not None
