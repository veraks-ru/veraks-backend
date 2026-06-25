"""Порт постановки фоновых задач (без знания о конкретном брокере).

Домен/прикладной слой ставит задачу через этот протокол; arq-реализация живёт
в воркере. Используется будущим триггером из домена resolutions: при финальном
разрешении события — поставить ``score_event``.

TODO(scoring-infra): подключить вызов из домена resolutions, когда он появится.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class TaskScheduler(Protocol):
    """Постановка фоновых задач скоринга в очередь."""

    async def enqueue_score_event(self, event_id: uuid.UUID) -> None:
        """Ставит задачу пер-прогнозного скоринга события."""
        ...
