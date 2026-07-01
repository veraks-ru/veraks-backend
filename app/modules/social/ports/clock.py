"""Порт часов домена social."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class Clock(Protocol):
    """Источник времени (сервер)."""

    def now(self) -> datetime: ...
