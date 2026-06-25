"""Адаптер часов — системное серверное время (UTC)."""

from __future__ import annotations

from datetime import datetime, timezone


class SystemClock:
    """Реализация порта ``Clock`` поверх системных часов."""

    def now(self) -> datetime:
        """Текущий момент времени в UTC."""
        return datetime.now(timezone.utc)
