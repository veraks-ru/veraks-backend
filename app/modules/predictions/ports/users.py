"""Порт резолва пользователя по публичному хэндлу (зависимость к identity).

Публичный трек-рекорд запрашивается по ``username``; домену прогнозов нужен
лишь перевод хэндла в ``user_id`` — и обратно, батчем по id, для доски лучших
прогнозов события. Реализация-адаптер читает таблицу users.

TODO(predictions-integration): прямое чтение соседней таблицы в монолите;
заменить сетевым контрактом при выделении identity в отдельный сервис.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class PublicUserRef:
    """Публичный хэндл активного пользователя — для доски лучших прогнозов."""

    user_id: uuid.UUID
    username: str
    display_name: str


@runtime_checkable
class UserDirectory(Protocol):
    """Резолв ``user_id`` по публичному хэндлу и обратно."""

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        """``id`` активного пользователя по username или ``None``."""
        ...

    async def list_active_by_ids(
        self, user_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, PublicUserRef]:
        """Хэндлы активных пользователей из ``user_ids`` одним запросом.

        Удалённые/заблокированные аккаунты в результат не попадают (доска
        лучших прогнозов их не показывает).
        """
        ...
