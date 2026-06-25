"""Доменные исключения events.

Все ошибки наследуются от ``EventError`` — это позволяет API-слою
единообразно маппить их в HTTP-ответы (см. ``app/main.py``), не завязываясь
на конкретику транспорта.
"""

from __future__ import annotations


class EventError(Exception):
    """Базовая ошибка домена events."""


class EventNotFoundError(EventError):
    """Запрошенное событие не найдено."""


class CategoryNotFoundError(EventError):
    """Указанная категория не существует."""


class InvalidEventWindowError(EventError):
    """Окна приёма/разрешения заданы некорректно (порядок дат, таймзона)."""


class InvalidEventDataError(EventError):
    """Обязательное текстовое поле пустое или не прошло валидацию."""


class InvalidEventTransitionError(EventError):
    """Недопустимый переход статуса в конечном автомате жизненного цикла."""


class EventEditNotAllowedError(EventError):
    """Редактирование запрещено в текущем статусе (или поле заблокировано)."""


class CategorySlugTakenError(EventError):
    """Нарушение ``UNIQUE(slug)`` категории."""


class EventPermissionError(EventError):
    """У актора недостаточно прав (RBAC) для операции над событиями."""
