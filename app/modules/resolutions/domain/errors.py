"""Доменные ошибки resolutions.

Все наследуют :class:`ResolutionError`; в HTTP-статусы маппятся централизованно
в ``app/main.py`` (обработчик + ``_ERROR_STATUS``), а не в роутере.
"""

from __future__ import annotations


class ResolutionError(Exception):
    """Базовая ошибка домена resolutions."""


class ResolutionTargetEventNotFoundError(ResolutionError):
    """Событие для разрешения/оспаривания не найдено."""


class ResolutionNotFoundError(ResolutionError):
    """У события нет текущего (финального) решения."""


class EventNotResolvableError(ResolutionError):
    """Событие нельзя разрешать в его текущем статусе (нужен ``closed``)."""


class InvalidResolutionDataError(ResolutionError):
    """Некорректные данные решения (пустой источник, нет исхода overturn'а)."""


class ResolutionPermissionError(ResolutionError):
    """Недостаточно прав (RBAC) для фиксации исхода/арбитража."""


class DisputeNotFoundError(ResolutionError):
    """Запрошенный спор не найден."""


class DisputeWindowClosedError(ResolutionError):
    """Оспаривание невозможно: событие не ``resolved`` или окно истекло."""


class DisputeNotAllowedError(ResolutionError):
    """Оспаривать вправе только участник (есть прогноз по событию)."""


class DisputeAlreadyDecidedError(ResolutionError):
    """Спор уже закрыт (accepted/rejected) — повторное решение запрещено."""


class SelfDisputeDecisionError(ResolutionError):
    """Нельзя решать собственный спор (разделение обязанностей)."""
