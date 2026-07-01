"""Доменные ошибки лиг/дивизионов."""

from __future__ import annotations


class LeagueError(Exception):
    """База доменных ошибок leagues."""


class InvalidLeagueDataError(LeagueError):
    """Некорректные данные лиги (имя/уровень)."""


class LeagueNotFoundError(LeagueError):
    """Лига не найдена (в т.ч. по коду приглашения)."""


class NotLeagueMemberError(LeagueError):
    """Действие требует членства в лиге."""


class LeaguePermissionError(LeagueError):
    """Нет прав на операцию с лигой (например, только владелец)."""


class DivisionNotFoundError(LeagueError):
    """Дивизион не найден."""
