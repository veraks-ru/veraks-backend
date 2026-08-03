"""identity: хэшировать esia_oid (152-ФЗ, минимизация ПДн).

Идентификатор гражданина в ЕСИА (``esia_oid``) до этой миграции хранился в
открытом виде — симметрично тому, как уже хранится СНИЛС (``snils_hash``,
миграция 0001), переводим и его на HMAC-SHA256: добавляем колонку
``esia_oid_hash``, бэкфиллим существующие строки, снимаем сырую ``esia_oid``.

Хэш считается тем же ключом, что и ``snils_hash``, но с доменным префиксом
HMAC-сообщения (``b"esia_oid:"``) — см. докстринг ``HmacEsiaOidHasher``
(app/modules/identity/adapters/security.py) с обоснованием, почему это не
приводит к совпадению хэшей СНИЛС и OID при равных «сырых» входах.

Бэкфиллу нужен рабочий HMAC-ключ приложения (``SECURITY_SNILS_HMAC_KEY``) —
без него миграция падает с понятной ошибкой, а не молча оставляет пустые
хэши (см. ``_esia_oid_hasher``).

Revision ID: 0024_hash_esia_oid
Revises: 0023_jump_payout_requisites
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.config import get_settings
from app.modules.identity.adapters.security import HmacEsiaOidHasher

revision = "0024_hash_esia_oid"
down_revision = "0023_jump_payout_requisites"
branch_labels = None
depends_on = None


def _esia_oid_hasher() -> HmacEsiaOidHasher:
    """HMAC-хешер oid тем же ключом, каким его собирает composition root.

    Явно проверяем доступность ключа, чтобы при отсутствующем окружении
    миграция падала с понятным сообщением, а не тихо пропускала бэкфилл
    (например, из-за AttributeError на None) или не проваливалась с
    невнятной pydantic-трассировкой.
    """
    try:
        key = get_settings().security.snils_hmac_key
    except Exception as exc:  # ValidationError pydantic и т.п. — окружение не готово
        raise RuntimeError(
            "Бэкфилл esia_oid_hash требует HMAC-ключ приложения "
            "(переменная окружения SECURITY_SNILS_HMAC_KEY) — не найден или "
            "невалиден в окружении миграции. Экспортируйте переменные "
            "окружения (см. backend/CLAUDE.md, alembic/env.py) и повторите "
            "`alembic upgrade`."
        ) from exc
    return HmacEsiaOidHasher(key)


def upgrade() -> None:
    """Добавляет esia_oid_hash → бэкфилл по существующим строкам → снимает esia_oid."""
    op.add_column("users", sa.Column("esia_oid_hash", sa.Text(), nullable=True))

    users = sa.table(
        "users",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("esia_oid", sa.Text()),
        sa.column("esia_oid_hash", sa.Text()),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(users.c.id, users.c.esia_oid)).fetchall()
    if rows:
        hasher = _esia_oid_hasher()
        for user_id, esia_oid in rows:
            bind.execute(
                users.update()
                .where(users.c.id == user_id)
                .values(esia_oid_hash=hasher.hash(esia_oid))
            )

    op.alter_column("users", "esia_oid_hash", nullable=False)
    op.drop_constraint("uq_users_esia_oid", "users", type_="unique")
    op.drop_column("users", "esia_oid")
    op.create_unique_constraint("uq_users_esia_oid_hash", "users", ["esia_oid_hash"])


def downgrade() -> None:
    """Восстанавливает форму схемы — сырой ``esia_oid`` необратимо потерян.

    HMAC — однонаправленная функция: исходный oid из ``esia_oid_hash`` не
    восстановить. Колонку возвращаем (для совместимости со старым кодом), но
    заполняем значением хэша как плейсхолдером — этого достаточно, чтобы
    удовлетворить NOT NULL/UNIQUE и откатить структуру; для настоящего отката
    данных нужен бэкап, снятый до upgrade.
    """
    op.add_column("users", sa.Column("esia_oid", sa.Text(), nullable=True))

    users = sa.table(
        "users",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("esia_oid", sa.Text()),
        sa.column("esia_oid_hash", sa.Text()),
    )
    bind = op.get_bind()
    rows = bind.execute(sa.select(users.c.id, users.c.esia_oid_hash)).fetchall()
    for user_id, esia_oid_hash in rows:
        bind.execute(
            users.update().where(users.c.id == user_id).values(esia_oid=esia_oid_hash)
        )

    op.alter_column("users", "esia_oid", nullable=False)
    op.drop_constraint("uq_users_esia_oid_hash", "users", type_="unique")
    op.drop_column("users", "esia_oid_hash")
    op.create_unique_constraint("uq_users_esia_oid", "users", ["esia_oid"])
