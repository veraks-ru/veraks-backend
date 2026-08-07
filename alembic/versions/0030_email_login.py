"""identity: email как способ входа + флаг подтверждённой личности

Договор с интегратором ЕСИА ещё не заключён, а запускаться нужно сейчас,
поэтому основным способом входа временно становится email с одноразовой
ссылкой. Схема до этой миграции предполагала, что аккаунт может появиться
ТОЛЬКО из ЕСИА: ``snils_hash``/``esia_oid_hash`` — NOT NULL + UNIQUE.

Что делаем:

1. ``users.email`` — ``citext`` (тот же тип, что у ``username``: адрес
   регистронезависим на практике у всех массовых провайдеров) + ЧАСТИЧНЫЙ
   уникальный индекс ``WHERE email IS NOT NULL``.
2. ``snils_hash``/``esia_oid_hash`` становятся nullable, а их UNIQUE-констрейнты
   заменяются на частичные уникальные индексы ``WHERE … IS NOT NULL``.
   Обычный UNIQUE в Postgres тоже пропускает несколько NULL-ов, но частичный
   индекс выражает намерение явно и не даёт случайно вернуть NOT NULL.
3. ``users.identity_verified`` — «личность подтверждена государственной
   идентификацией». PRD §7 связывает выплату приза с идентификацией личности:
   участие открыто всем, а вот статус платформа обязана различать. Всем
   существующим строкам ставим ``true`` — они все пришли из ЕСИА; новым
   email-аккаунтам ``false`` (server_default).

Revision ID: 0030_email_login
Revises: 0029_extend_append_only_revoke
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_email_login"
down_revision: str | None = "0029_extend_append_only_revoke"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет email/identity_verified и делает ключи ЕСИА необязательными."""
    op.add_column("users", sa.Column("email", postgresql.CITEXT(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "identity_verified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    # Все существующие аккаунты заведены через ЕСИА — их личность подтверждена.
    op.execute("UPDATE users SET identity_verified = true")

    op.alter_column("users", "snils_hash", existing_type=sa.Text(), nullable=True)
    op.alter_column("users", "esia_oid_hash", existing_type=sa.Text(), nullable=True)

    # UNIQUE-констрейнты → частичные уникальные индексы (уникален заполненный).
    op.drop_constraint("uq_users_snils_hash", "users", type_="unique")
    op.drop_constraint("uq_users_esia_oid_hash", "users", type_="unique")
    op.create_index(
        "ux_users_snils_hash",
        "users",
        ["snils_hash"],
        unique=True,
        postgresql_where=sa.text("snils_hash IS NOT NULL"),
    )
    op.create_index(
        "ux_users_esia_oid_hash",
        "users",
        ["esia_oid_hash"],
        unique=True,
        postgresql_where=sa.text("esia_oid_hash IS NOT NULL"),
    )
    op.create_index(
        "ux_users_email",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL"),
    )


def downgrade() -> None:
    """Возвращает NOT NULL + UNIQUE на ключи ЕСИА и снимает email.

    Откат возможен только если в базе нет аккаунтов без ``snils_hash``
    (то есть заведённых по email) — иначе ``ALTER … SET NOT NULL`` упадёт, и
    это правильно: молча удалять аккаунты живых участников миграция не вправе.
    """
    op.drop_index("ux_users_email", table_name="users")
    op.drop_index("ux_users_esia_oid_hash", table_name="users")
    op.drop_index("ux_users_snils_hash", table_name="users")

    op.alter_column("users", "esia_oid_hash", existing_type=sa.Text(), nullable=False)
    op.alter_column("users", "snils_hash", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint("uq_users_esia_oid_hash", "users", ["esia_oid_hash"])
    op.create_unique_constraint("uq_users_snils_hash", "users", ["snils_hash"])

    op.drop_column("users", "identity_verified")
    op.drop_column("users", "email")
