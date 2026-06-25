"""Порт часов — единый источник серверного времени для домена scoring.

Вынесение ``now()`` за порт делает проставление ``scored_at``/``updated_at``
детерминированно тестируемым. Каждый домен владеет своим ``Clock``, оставаясь
независимым.
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
