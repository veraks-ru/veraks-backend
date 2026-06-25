"""Порты-шлюзы seasons к данным других доменов.

``DisputeGuard`` — проверка открытых споров по событиям сезона перед
финализацией. Домен resolutions/disputes ещё не построен, поэтому боевой
адаптер — заглушка ``AlwaysAllowsDisputeGuard`` (fail-loud, см. дизайн §6.4).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class DisputeGuard(Protocol):
    """Есть ли открытые споры по событиям сезона (блокируют финализацию)."""

    async def has_open_disputes(self, season_id: uuid.UUID) -> bool:
        """``True``, если по событиям сезона есть незакрытые споры."""
        ...
