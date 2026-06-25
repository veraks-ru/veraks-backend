"""Системные часы resolutions (timezone-aware, UTC)."""

from __future__ import annotations

from datetime import datetime, timezone


class SystemClock:
    """Источник времени — сервер; всегда UTC."""

    def now(self) -> datetime:
        """Текущий момент в UTC."""
        return datetime.now(timezone.utc)
