"""Порт подписочного гейта для events.

Предлагать события может только пользователь с активной подпиской (как и
голосовать). Реализация смотрит на домен billing; структурно совместима с
адаптером из predictions (тот же запрос к подпискам).
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
