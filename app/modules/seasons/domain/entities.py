"""Доменная сущность ``Season`` и перечисление статусов.

Сезон — соревновательный период с замороженным набором правил
(:class:`~app.modules.seasons.domain.value_objects.LeagueConfig`), который
снимается при активации. Переходы статусов — через методы сущности,
делегирующие чистым правилам из :mod:`lifecycle`; повтор перехода идемпотентен.

Обычный mutable-dataclass без знания о SQLAlchemy/pydantic (как ``User`` и
``Rating``); ORM маппится на него явными ``to_domain``/``from_domain``.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.seasons.domain import lifecycle
from app.modules.seasons.domain.value_objects import LeagueConfig


class SeasonStatus(str, enum.Enum):
    """Жизненный цикл сезона."""

    UPCOMING = "upcoming"
    ACTIVE = "active"
    FINISHED = "finished"


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Season:
    """Соревновательный сезон с замороженной конфигурацией лиги.

    ``league_config`` — ``None`` до активации; при ``upcoming → active``
    снимается переданный извне снапшот и далее не меняется.
    """

    slug: str
    title: str
    starts_at: datetime
    ends_at: datetime
    status: SeasonStatus = SeasonStatus.UPCOMING
    league_config: LeagueConfig | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def activate(self, config: LeagueConfig, *, now: datetime | None = None) -> bool:
        """Переводит ``upcoming → active``, замораживая ``config``.

        Возвращает ``True``, если переход состоялся; ``False`` — если сезон уже
        активен (идемпотентный no-op, правила сезона неизменны). Поднимает
        :class:`InvalidSeasonTransitionError` из ``finished``.
        """
        if lifecycle.is_noop(self.status, SeasonStatus.ACTIVE):
            return False
        lifecycle.ensure_transition_allowed(self.status, SeasonStatus.ACTIVE)
        self.status = SeasonStatus.ACTIVE
        self.league_config = config
        self.updated_at = now or _utcnow()
        return True

    def finalize(self, *, now: datetime | None = None) -> bool:
        """Переводит ``active → finished``.

        Возвращает ``True``, если переход состоялся; ``False`` — если сезон уже
        завершён (идемпотентный no-op: повтор не пересчитывает результат).
        Поднимает :class:`InvalidSeasonTransitionError` из ``upcoming``.
        """
        if lifecycle.is_noop(self.status, SeasonStatus.FINISHED):
            return False
        lifecycle.ensure_transition_allowed(self.status, SeasonStatus.FINISHED)
        self.status = SeasonStatus.FINISHED
        self.updated_at = now or _utcnow()
        return True
