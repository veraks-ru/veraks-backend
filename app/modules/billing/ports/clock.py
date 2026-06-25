"""Порт источника времени."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Источник текущего момента (timezone-aware, UTC)."""

    def now(self) -> datetime:
        """Текущий момент в UTC."""
        ...
