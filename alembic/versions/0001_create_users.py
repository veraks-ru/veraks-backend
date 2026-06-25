"""identity: создание таблицы users

Revision ID: 0001_create_users
Revises:
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_create_users"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = postgresql.ENUM(
    "user", "editor", "arbiter", "admin", name="user_role", create_type=False
)
user_status = postgresql.ENUM(
    "active", "suspended", "deleted", name="user_status", create_type=False
)


def upgrade() -> None:
    """Расширение citext, enum-типы и таблица users с индексами."""
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    bind = op.get_bind()
    user_role.create(bind, checkfirst=True)
    user_status.create(bind, checkfirst=True)

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("esia_oid", sa.Text(), nullable=False),
        sa.Column("snils_hash", sa.Text(), nullable=False),
        sa.Column("username", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("real_name_enc", sa.LargeBinary(), nullable=True),
        sa.Column("role", user_role, nullable=False, server_default="user"),
        sa.Column("status", user_status, nullable=False, server_default="active"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    # Ядро инварианта «1 человек = 1 аккаунт» и быстрых выборок.
    op.create_unique_constraint("uq_users_esia_oid", "users", ["esia_oid"])
    op.create_unique_constraint("uq_users_snils_hash", "users", ["snils_hash"])
    op.create_unique_constraint("uq_users_username", "users", ["username"])
    op.create_index("ix_users_status", "users", ["status"])


def downgrade() -> None:
    """Откат таблицы и enum-типов."""
    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
    bind = op.get_bind()
    user_status.drop(bind, checkfirst=True)
    user_role.drop(bind, checkfirst=True)
