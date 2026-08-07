"""Юнит-тест маппинга ``UserORM`` ↔ доменный ``User``.

Регрессия: колонка/атрибут ``esia_oid`` (сырой идентификатор ЕСИА) больше не
существует — ни в ORM-модели, ни в доменной сущности, только их HMAC-хеш
``esia_oid_hash``. Тест не трогает БД (конструирует ORM-объект напрямую).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.modules.identity.adapters.orm import UserORM
from app.modules.identity.domain.entities import User, UserRole, UserStatus


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        esia_oid_hash="deadbeef" * 8,
        snils_hash="cafebabe" * 8,
        username="mapping-test",
        display_name="Маппинг-тест",
        real_name_enc=None,
        role=UserRole.USER,
        status=UserStatus.ACTIVE,
        created_at=datetime.now(UTC),
    )


def test_orm_has_no_raw_esia_oid_attribute() -> None:
    """Регрессия: сырой esia_oid нигде не персистится — атрибута просто нет."""
    orm = UserORM.from_domain(_user())
    assert not hasattr(orm, "esia_oid")


def test_from_domain_to_domain_roundtrip_keeps_only_hash() -> None:
    """``from_domain``/``to_domain`` переносят именно хэш, без искажений."""
    user = _user()
    orm = UserORM.from_domain(user)
    assert orm.esia_oid_hash == user.esia_oid_hash

    restored = orm.to_domain()
    assert restored.esia_oid_hash == user.esia_oid_hash
    assert restored.esia_oid_hash != "esia-raw-oid"
