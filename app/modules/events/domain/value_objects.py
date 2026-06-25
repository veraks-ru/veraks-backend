"""Value-objects домена events.

Чистый код без I/O — легко покрывается юнит-тестами в изоляции от FastAPI
и БД. Здесь живут инварианты временного окна события и формата slug'а
категории.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from app.modules.events.domain.errors import (
    InvalidEventDataError,
    InvalidEventWindowError,
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class EventWindow:
    """Временное окно события: приём прогнозов и ожидаемое разрешение.

    Инварианты (источник времени — сервер, все значения timezone-aware):
        ``opens_at < closes_at <= resolves_at``.

    ``opens_at`` — старт приёма прогнозов, ``closes_at`` — жёсткая блокировка
    (после неё прогнозы неизменяемы), ``resolves_at`` — ожидаемая дата
    подведения исхода.
    """

    opens_at: datetime
    closes_at: datetime
    resolves_at: datetime

    def __post_init__(self) -> None:
        for label, value in (
            ("opens_at", self.opens_at),
            ("closes_at", self.closes_at),
            ("resolves_at", self.resolves_at),
        ):
            if value.tzinfo is None:
                raise InvalidEventWindowError(
                    f"{label} должен быть timezone-aware (источник времени — сервер)"
                )
        if self.opens_at >= self.closes_at:
            raise InvalidEventWindowError("opens_at должен быть строго раньше closes_at")
        if self.closes_at > self.resolves_at:
            raise InvalidEventWindowError("resolves_at не может быть раньше closes_at")

    def is_accepting_at(self, moment: datetime) -> bool:
        """Открыт ли приём прогнозов в указанный момент времени."""
        return self.opens_at <= moment < self.closes_at


def validate_slug(raw: str) -> str:
    """Нормализует и проверяет slug категории (``kebab-case``, латиница/цифры).

    Возвращает очищенный slug либо поднимает :class:`InvalidEventDataError`.
    Уникальность обеспечивается на уровне БД (``UNIQUE(slug)``).
    """
    slug = raw.strip().lower()
    if not slug:
        raise InvalidEventDataError("slug категории не может быть пустым")
    if not _SLUG_RE.match(slug):
        raise InvalidEventDataError(
            "slug допускает только латиницу, цифры и дефис (kebab-case)"
        )
    return slug


def require_text(raw: str, *, field: str, max_length: int = 10_000) -> str:
    """Проверяет обязательное текстовое поле и возвращает обрезанное значение."""
    value = raw.strip()
    if not value:
        raise InvalidEventDataError(f"Поле «{field}» обязательно и не может быть пустым")
    if len(value) > max_length:
        raise InvalidEventDataError(f"Поле «{field}» превышает {max_length} символов")
    return value
