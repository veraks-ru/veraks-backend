"""Доменные ошибки соцфич (маппятся в HTTP на границе)."""

from __future__ import annotations


class SocialError(Exception):
    """База доменных ошибок social."""


class CommentEmptyError(SocialError):
    """Пустой комментарий."""


class CommentTooLongError(SocialError):
    """Слишком длинный комментарий."""


class CommentNotFoundError(SocialError):
    """Комментарий не найден."""


class CommentForbiddenError(SocialError):
    """Нет прав на действие с комментарием (не автор и не модератор)."""


class CommentEventNotFoundError(SocialError):
    """Комментируемое событие не существует."""


class SelfFollowError(SocialError):
    """Попытка подписаться на самого себя."""


class FollowTargetNotFoundError(SocialError):
    """Целевой пользователь для подписки не найден."""
