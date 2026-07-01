"""Доменные ошибки B2B signal API."""

from __future__ import annotations


class B2bError(Exception):
    """База доменных ошибок b2b."""


class InvalidB2bDataError(B2bError):
    """Некорректные данные ключа."""


class ApiKeyNotFoundError(B2bError):
    """Ключ не найден (или не принадлежит владельцу)."""


class InvalidApiKeyError(B2bError):
    """Предъявленный ключ неизвестен или отозван (→ 401)."""


class QuotaExceededError(B2bError):
    """Исчерпана суточная квота ключа (→ 429)."""


class SignalTargetNotFoundError(B2bError):
    """Цель сигнала (событие) не найдена (→ 404)."""
