"""Порт резолва пользователя по публичному хэндлу (зависимость к identity).

Публичная калибровка профиля запрашивается по ``username`` (контракт API
задания), а скоринг оперирует ``user_id``. Реализация-адаптер читает таблицу
users; домен скоринга об устройстве identity не знает.

TODO(scoring-integration): прямое чтение соседней таблицы в монолите; заменить
сетевым контрактом при выделении identity в отдельный сервис.
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
