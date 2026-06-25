"""Доменные ошибки скоринга.

Наследуются от базового :class:`ScoringError`, который централизованно
маппится в HTTP-статусы в ``app/main.py`` (как и ошибки других доменов).
Чисто доменные нарушения инвариантов (например, недостаточно предсказателей
для leave-one-out) — тоже здесь, без знания о транспорте.
"""

from __future__ import annotations


class ScoringError(Exception):
    """Базовая ошибка домена scoring."""


class NotEnoughPredictorsError(ScoringError):
    """Недостаточно предсказателей для бенчмарка leave-one-out (нужно ≥ 2)."""


class EventNotResolvedError(ScoringError):
    """Скоринг события невозможен: исход ещё не зафиксирован/не финализирован."""


class ScoringTargetEventNotFoundError(ScoringError):
    """Событие для скоринга не найдено."""


class RatingNotFoundError(ScoringError):
    """Запрошенный рейтинг (профиль/лидерборд) не найден."""


class ScoringPermissionError(ScoringError):
    """Недостаточно прав для запуска скоринга/пересчёта рейтингов."""
