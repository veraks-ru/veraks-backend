"""Порт резолва пользователя по публичному хэндлу (зависимость к identity).

Публичный трек-рекорд запрашивается по ``username``; домену прогнозов нужен
лишь перевод хэндла в ``user_id``. Реализация-адаптер читает таблицу users.

TODO(predictions-integration): прямое чтение соседней таблицы в монолите;
заменить сетевым контрактом при выделении identity в отдельный сервис.
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class UserDirectory(Protocol):
    """Резолв ``user_id`` по публичному хэндлу."""

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        """``id`` активного пользователя по username или ``None``."""
        ...
