"""Порт подписочного гейта.

Голосовать (ставить прогноз) может только пользователь с активной подпиской.
Реализация смотрит на домен billing; в тестах подменяется фейком.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class SubscriptionGate(Protocol):
    """Есть ли у пользователя активная подписка на момент ``now``."""

    async def has_active_subscription(
        self, user_id: uuid.UUID, now: datetime
    ) -> bool:
        ...
