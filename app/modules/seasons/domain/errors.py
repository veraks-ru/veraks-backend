"""Доменные ошибки сезонов.

Наследуются от базовой :class:`SeasonError`, которая централизованно маппится
в HTTP-статусы в ``app/main.py`` (как и ошибки других доменов). Чисто доменные
нарушения инвариантов (недопустимый переход, провал квалификации правил) —
тоже здесь, без знания о транспорте.
"""

from __future__ import annotations


class SeasonError(Exception):
    """Базовая ошибка домена seasons."""


class SeasonNotFoundError(SeasonError):
    """Запрошенный сезон не найден (по id или slug)."""


class SeasonSlugTakenError(SeasonError):
    """Slug сезона уже занят (нарушение ``UNIQUE(slug)``)."""


class InvalidSeasonDataError(SeasonError):
    """Некорректные данные сезона/конфигурации лиги (валидация до БД)."""


class InvalidSeasonTransitionError(SeasonError):
    """Недопустимый переход жизненного цикла сезона."""


class SeasonPermissionError(SeasonError):
    """Недостаточно прав для управления сезоном/перевода статуса."""


class SeasonFinalizationBlockedError(SeasonError):
    """Финализация невозможна: по событиям сезона есть открытые споры.

    Финализация на нефинальных исходах = расчёт призов по исходам, которые ещё
    могут измениться. См. дизайн §6.4 и порт ``DisputeGuard``.
    """
