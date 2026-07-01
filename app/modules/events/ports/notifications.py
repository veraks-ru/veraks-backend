"""Порт нотификатора для events (модерация уведомляет автора предложения).

Структурно совпадает с эмиттером домена notifications; композит-рут events
связывает их (как с подписочным гейтом).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    async def emit(
        self,
        *,
        user_id: uuid.UUID,
        kind: str,
        title: str,
        body: str = "",
        entity_type: str | None = None,
        entity_id: uuid.UUID | None = None,
    ) -> None: ...
