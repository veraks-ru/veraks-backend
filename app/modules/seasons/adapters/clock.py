"""Системные часы — реализация порта :class:`Clock` (UTC)."""

from __future__ import annotations

from datetime import datetime, timezone


class SystemClock:
    """Часы на основе системного времени (timezone-aware, UTC)."""

    def now(self) -> datetime:
        """Текущий момент в UTC."""
        return datetime.now(timezone.utc)
