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


class RestrictedCategoryError(EventError):
    """Категория запрещена для событий (PRD §7.5).

    Поднимается при создании (``CreateEvent``) или предложении
    (``ProposeEvent``) события в категории с ``is_restricted=true``: смерть/
    здоровье конкретных лиц, насилие, теракты, экстремизм, частная жизнь —
    такие темы не проходят модерацию по определению.
    """


class EventPermissionError(EventError):
    """У актора недостаточно прав (RBAC) для операции над событиями."""


class EventSubscriptionRequiredError(EventError):
    """Предложить событие можно только с активной подпиской."""
