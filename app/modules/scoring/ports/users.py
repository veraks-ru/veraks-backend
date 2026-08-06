"""Порт справочника пользователей (зависимость к identity).

Публичная калибровка профиля запрашивается по ``username`` (контракт API
задания), а скоринг оперирует ``user_id``; лидербордам нужна обратная
проверка — какие из ``user_id`` принадлежат публично видимым (ACTIVE)
аккаунтам. Реализация-адаптер читает таблицу users; домен скоринга об
устройстве identity не знает.

TODO(scoring-integration): прямое чтение соседней таблицы в монолите; заменить
сетевым контрактом при выделении identity в отдельный сервис.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Protocol, runtime_checkable


@runtime_checkable
class UserDirectory(Protocol):
    """Резолв ``user_id`` по публичному хэндлу и отбор публично видимых id."""

    async def resolve_username(self, username: str) -> uuid.UUID | None:
        """``id`` активного пользователя по username или ``None``."""
        ...

    async def list_active_ids(
        self, user_ids: Sequence[uuid.UUID]
    ) -> set[uuid.UUID]:
        """Подмножество ``user_ids``, принадлежащее активным аккаунтам.

        Одним запросом (батч на страницу лидерборда). Удалённые/заблокированные
        аккаунты в результат не попадают — их строки лидерборда скрываются
        (публичный профиль по ним недоступен, ссылка была бы мёртвой).
        """
        ...
