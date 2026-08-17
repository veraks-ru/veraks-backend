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
from datetime import UTC, datetime

from app.modules.seasons.domain import lifecycle
from app.modules.seasons.domain.errors import InvalidSeasonTransitionError
from app.modules.seasons.domain.value_objects import LeagueConfig


class SeasonStatus(str, enum.Enum):
    """Жизненный цикл сезона."""

    UPCOMING = "upcoming"
    ACTIVE = "active"
    FINISHED = "finished"


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(UTC)


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
    # Правила, выбранные заранее: их заморозит активация, включая
    # автоматическую. Правятся свободно, пока сезон ``upcoming``; после
    # активации смысл теряют — источником истины становится ``league_config``.
    planned_league_config: LeagueConfig | None = None
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

    def repair_rules(
        self, config: LeagueConfig, *, now: datetime | None = None
    ) -> None:
        """Заменяет замороженные правила активного сезона.

        Обычно правила неизменны: участники полагаются на объявленные условия
        (ст. 1058 ГК, PRD §7). Но сезон может активироваться автоматически —
        воркер поднимает ``upcoming`` сезон, у которого наступил ``starts_at``,
        и замораживает конфигурацию по умолчанию. Если ``starts_at`` был задан
        в прошлом, это происходит через минуты после создания, и человек
        физически не успевает выбрать пороги.

        Пока по сезону нет ни одного прогноза, полагаться на его условия
        некому: конкурс объявлен, но не начался. Тогда исправить неудачно
        замороженные пороги — честнее, чем оставить сезон, в котором к призам
        не может пройти никто.

        **Проверку отсутствия прогнозов делает вызывающий слой** (``application``):
        домену неоткуда узнать о прогнозах, они в другом контексте.
        """
        if self.status is not SeasonStatus.ACTIVE:
            raise InvalidSeasonTransitionError(
                "Исправление правил возможно только для активного сезона"
            )
        self.league_config = config
        self.updated_at = now or _utcnow()

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
