"""Порт часов — единый источник серверного времени для домена прогнозов.

Вынесение ``now()`` за порт делает проверку дедлайна (``closes_at``)
детерминированно тестируемой. Домен events держит свой ``Clock`` — каждый
домен владеет своим портом, чтобы оставаться независимым.
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
