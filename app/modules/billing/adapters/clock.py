"""Системные часы billing."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemClock:
    """Источник серверного времени; всегда UTC."""

    def now(self) -> datetime:
        """Текущий момент в UTC."""
        return datetime.now(UTC)
