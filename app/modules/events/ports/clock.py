"""Порт часов — единый источник серверного времени.

Время в системе задаёт сервер (см. конвенции модели данных). Вынесение
``now()`` за порт делает переходы жизненного цикла детерминированно
тестируемыми (в тестах подставляется фиксированные часы).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Источник текущего момента времени (timezone-aware, UTC)."""

    def now(self) -> datetime:
        """Возвращает текущий момент в UTC."""
        ...
