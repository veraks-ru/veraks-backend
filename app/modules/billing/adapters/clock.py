"""Системные часы billing."""

from __future__ import annotations

from datetime import datetime, timezone


class SystemClock:
    """Источник серверного времени; всегда UTC."""

    def now(self) -> datetime:
        """Текущий момент в UTC."""
        return datetime.now(timezone.utc)
