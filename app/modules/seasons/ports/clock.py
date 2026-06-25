"""Порт часов — источник текущего момента времени (timezone-aware, UTC)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Источник текущего момента времени (UTC)."""

    def now(self) -> datetime:
        """Возвращает текущий момент в UTC."""
        ...
