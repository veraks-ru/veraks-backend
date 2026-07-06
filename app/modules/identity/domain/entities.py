"""Доменная сущность ``User`` и связанные перечисления.

Сущность намеренно не знает ни о SQLAlchemy, ни о pydantic — это обычный
dataclass. ORM-модель (adapters/orm.py) и API-схемы (api/schemas.py)
мапятся на неё, а не наоборот.
"""

from __future__ import annotations

import enum
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.modules.identity.domain.value_objects import EsiaIdentity


class UserRole(str, enum.Enum):
    """RBAC-роли (см. раздел безопасности: разделение обязанностей)."""

    USER = "user"
    EDITOR = "editor"
    ARBITER = "arbiter"
    ADMIN = "admin"


class UserStatus(str, enum.Enum):
    """Жизненный цикл аккаунта."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    DELETED = "deleted"


def _utcnow() -> datetime:
    """Текущее время в UTC (источник времени — сервер)."""
    return datetime.now(timezone.utc)


def generate_username_seed() -> str:
    """Псевдонимный хэндл, НЕ производный от ФИО (приватность, PRD §4.1/§7.6).

    Реальное имя не попадает в публичный идентификатор (раньше хэндл строился из
    ФИО, а для кириллических имён вырождался в ``predictor``, деанонимизируя через
    display_name). Уникальность — на уровне БД (``UNIQUE(username)``); случайный
    хвост делает коллизии крайне маловероятными, но use-case всё равно
    переаллоцирует при UNIQUE-гонке.
    """
    return f"predictor-{secrets.token_hex(3)}"


@dataclass(slots=True)
class User:
    """Аккаунт, привязанный к верифицированному гражданину.

    ``snils_hash`` (HMAC от СНИЛС) — ключ инварианта «1 человек = 1 аккаунт».
    ``real_name_enc`` — зашифрованное ФИО; в публичный профиль не попадает.
    """

    esia_oid: str
    snils_hash: str
    username: str
    display_name: str
    real_name_enc: bytes | None
    role: UserRole = UserRole.USER
    status: UserStatus = UserStatus.ACTIVE
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def register_from_esia(
        cls,
        *,
        identity: EsiaIdentity,
        snils_hash: str,
        username: str,
        real_name_enc: bytes | None,
    ) -> User:
        """Фабрика нового аккаунта по данным ЕСИА (find-or-create: ветка create).

        ``display_name`` по умолчанию = псевдонимный ``username``: реальное ФИО
        (``real_name_enc``) публично не раскрывается (PRD §4.1/§7.6). Пользователь
        может задать отображаемое имя сам через ``PATCH /users/me``.
        """
        return cls(
            esia_oid=identity.oid,
            snils_hash=snils_hash,
            username=username,
            display_name=username,
            real_name_enc=real_name_enc,
        )

    def is_active(self) -> bool:
        """Может ли аккаунт пользоваться системой."""
        return self.status is UserStatus.ACTIVE

    def edit_profile(self, *, display_name: str | None) -> bool:
        """Редактирует публичный профиль (то, чем владеет пользователь).

        ``display_name`` — пользовательское поле (PATCH /users/me), поэтому при
        повторном входе ЕСИА его НЕ перезатирает (юридическое ФИО — отдельно в
        ``real_name_enc``). Возвращает ``True``, если значение изменилось.
        """
        if display_name is None:
            return False
        new_value = display_name.strip()
        if not new_value or new_value == self.display_name:
            return False
        self.display_name = new_value
        return True

    def apply_esia_refresh(
        self, *, identity: EsiaIdentity, real_name_enc: bytes | None
    ) -> bool:
        """Обновляет данные при повторном входе (ЕСИА — источник истины по ФИО).

        Возвращает ``True``, если что-то изменилось (нужен ли UPDATE/аудит).
        Хэндл (username) пользователь меняет сам — здесь его не трогаем.
        """
        changed = False
        if self.esia_oid != identity.oid:
            # OID привязки не должен меняться при том же snils_hash, но фиксируем.
            self.esia_oid = identity.oid
            changed = True
        if real_name_enc is not None and real_name_enc != self.real_name_enc:
            self.real_name_enc = real_name_enc
            changed = True
        return changed
