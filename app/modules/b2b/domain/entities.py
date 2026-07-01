"""Доменные сущности B2B signal API.

``ApiKey`` — ключ доступа B2B-потребителя к сигналам. Хранится только ХЭШ
секрета (как пароль); полный ключ показывается один раз при выдаче. ``key_prefix``
— первые символы для узнавания в списке. Чистый код без I/O.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.b2b.domain.errors import InvalidB2bDataError

_MAX_NAME_LEN = 80


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class ApiKey:
    """API-ключ B2B-потребителя (хранится хэш секрета, не сам секрет)."""

    owner_user_id: uuid.UUID
    name: str
    key_prefix: str
    key_hash: str
    daily_quota: int
    is_active: bool = True
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)
    revoked_at: datetime | None = None

    @classmethod
    def issue(
        cls,
        *,
        owner_user_id: uuid.UUID,
        name: str,
        key_prefix: str,
        key_hash: str,
        daily_quota: int,
        now: datetime | None = None,
    ) -> ApiKey:
        clean = name.strip()
        if not clean:
            raise InvalidB2bDataError("Название ключа не может быть пустым")
        if len(clean) > _MAX_NAME_LEN:
            raise InvalidB2bDataError(f"Название длиннее {_MAX_NAME_LEN} символов")
        if daily_quota < 1:
            raise InvalidB2bDataError("Суточная квота должна быть ≥ 1")
        return cls(
            owner_user_id=owner_user_id,
            name=clean,
            key_prefix=key_prefix,
            key_hash=key_hash,
            daily_quota=daily_quota,
            created_at=now or _utcnow(),
        )

    def revoke(self, *, now: datetime | None = None) -> None:
        self.is_active = False
        self.revoked_at = now or _utcnow()
