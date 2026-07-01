"""Порт нотификатора для billing (уведомление о подтверждённой выплате).

Структурно совпадает с эмиттером домена notifications; композит-рут связывает.
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
