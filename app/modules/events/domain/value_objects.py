"""Value-objects домена events.

Чистый код без I/O — легко покрывается юнит-тестами в изоляции от FastAPI
и БД. Здесь живут инварианты временного окна события и формата slug'а
категории.
"""

from __future__ import annotations

import base64
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.modules.events.domain.errors import (
    InvalidEventDataError,
    InvalidEventWindowError,
    InvalidFeedCursorError,
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Публичный код события — то, что человек видит в ссылке вместо UUID.
#
# Восемь случайных байт в base64url дают ровно 11 символов без выравнивания —
# тот же формат, что у ссылок YouTube. Код именно случайный, а не производный
# от заголовка: заголовок правят (событие уже переезжало с «приедет ли в
# Россию» на «концерт в Петербурге»), и ссылка, разосланная подписчикам, от
# этого не должна ломаться.
#
# 64 бита — вероятность совпадения ничтожна даже на миллионах событий, но
# UNIQUE в БД всё равно стоит: при столкновении лучше отказ, чем подмена
# чужого события.
PUBLIC_CODE_LENGTH = 11
_PUBLIC_CODE_RE = re.compile(rf"^[A-Za-z0-9_-]{{{PUBLIC_CODE_LENGTH}}}$")


def new_public_code() -> str:
    """Свежий публичный код события (11 символов base64url)."""
    return secrets.token_urlsafe(8)


def is_public_code(raw: str) -> bool:
    """Похожа ли строка на публичный код — в отличие от UUID или мусора."""
    return bool(_PUBLIC_CODE_RE.match(raw))


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


@dataclass(frozen=True, slots=True)
class FeedCursor:
    """Keyset-курсор страницы ленты: последняя выданная пара ``(closes_at, id)``.

    Самодостаточен — не хранит смещение, поэтому вставки и закрытия событий
    между запросами страниц не сбивают порядок (см. §6 дизайн-спеки ленты).
    Непрозрачен снаружи: клиент лишь передаёт его обратно как есть.
    """

    closes_at: datetime
    event_id: uuid.UUID

    def encode(self) -> str:
        """Base64url без паддинга от ``"<isoformat>|<uuid>"``."""
        raw = f"{self.closes_at.isoformat()}|{self.event_id}"
        token = base64.urlsafe_b64encode(raw.encode("utf-8"))
        return token.decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, raw: str) -> FeedCursor:
        """Разбирает курсор; любой мусор — :class:`InvalidFeedCursorError`.

        «Мусор» — это невалидный base64/UTF-8, не ровно две части при
        разбиении по ``|``, наивная (без таймзоны) дата или нераспознаваемый
        UUID. Курсор приходит от клиента непрозрачным, поэтому ошибка здесь
        не должна протекать наружу как внутренний сбой (500).
        """
        padded = raw + "=" * (-len(raw) % 4)
        try:
            decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        except ValueError as exc:
            raise InvalidFeedCursorError("Курсор страницы повреждён") from exc

        parts = decoded.split("|")
        if len(parts) != 2:
            raise InvalidFeedCursorError("Курсор страницы повреждён")
        closes_at_raw, event_id_raw = parts

        try:
            closes_at = datetime.fromisoformat(closes_at_raw)
        except ValueError as exc:
            raise InvalidFeedCursorError("Курсор страницы повреждён") from exc
        if closes_at.tzinfo is None:
            raise InvalidFeedCursorError("Курсор страницы повреждён")

        try:
            event_id = uuid.UUID(event_id_raw)
        except ValueError as exc:
            raise InvalidFeedCursorError("Курсор страницы повреждён") from exc

        return cls(closes_at=closes_at, event_id=event_id)


def require_text(raw: str, *, field: str, max_length: int = 10_000) -> str:
    """Проверяет обязательное текстовое поле и возвращает обрезанное значение."""
    value = raw.strip()
    if not value:
        raise InvalidEventDataError(f"Поле «{field}» обязательно и не может быть пустым")
    if len(value) > max_length:
        raise InvalidEventDataError(f"Поле «{field}» превышает {max_length} символов")
    return value
