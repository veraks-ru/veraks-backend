"""Доменные сущности лиг и дивизионов.

Приватные лиги — пользовательские группы с собственным лидербордом по коду
приглашения. Дивизионы — системная лестница уровней (1 = высший) с membership
пользователя на сезон; повышение/понижение считается при финализации сезона.
Чистый код без I/O.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.leagues.domain.errors import InvalidLeagueDataError

_MAX_NAME_LEN = 80


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class League:
    """Приватная лига: владелец + код приглашения + участники (через membership)."""

    name: str
    owner_id: uuid.UUID
    invite_code: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        owner_id: uuid.UUID,
        invite_code: str,
        now: datetime | None = None,
    ) -> League:
        clean = name.strip()
        if not clean:
            raise InvalidLeagueDataError("Название лиги не может быть пустым")
        if len(clean) > _MAX_NAME_LEN:
            raise InvalidLeagueDataError(
                f"Название длиннее {_MAX_NAME_LEN} символов"
            )
        return cls(
            name=clean,
            owner_id=owner_id,
            invite_code=invite_code,
            created_at=now or _utcnow(),
        )


@dataclass(slots=True)
class LeagueMembership:
    """Участие пользователя в приватной лиге."""

    league_id: uuid.UUID
    user_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    joined_at: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class Division:
    """Уровень системной лестницы (``level`` 1 = высший дивизион)."""

    level: int
    title: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.level < 1:
            raise InvalidLeagueDataError("Уровень дивизиона должен быть ≥ 1")


@dataclass(slots=True)
class DivisionMembership:
    """Дивизион пользователя в конкретном сезоне."""

    user_id: uuid.UUID
    season_id: uuid.UUID
    division_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
