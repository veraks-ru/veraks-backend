"""Порт постановки фоновых задач скоринга.

Локальный для домена контракт (resolutions не зависит от пакета scoring).
Структурно совпадает с ``scoring``-планировщиком, поэтому один и тот же
``ArqTaskScheduler`` удовлетворяет обоим (Protocol, duck typing).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskScheduler(Protocol):
    """Постановка пер-прогнозного скоринга события в очередь."""

    async def enqueue_score_event(self, event_id: uuid.UUID) -> None:
        """Ставит задачу ``score_event`` по событию."""
        ...
