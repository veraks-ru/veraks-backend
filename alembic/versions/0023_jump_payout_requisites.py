"""Выплаты Jump.Finance: провайдер ``jump`` и реквизиты выплат (СБП).

Добавляет значение ``jump`` в enum ``payment_provider`` и таблицу
``payout_requisites`` — реквизиты выплат пользователя (одна запись на
пользователя, UNIQUE(user_id)). ПДн (телефон, ФИО) лежат шифрованными
(Fernet, ключ SECURITY_FIELD_ENCRYPTION_KEY) — в открытом виде только
``sbp_bank_id`` (id банка из словаря СБП провайдера).

Значение ``'jump'`` в этой миграции НЕ используется: добавленное в enum
значение нельзя применять в той же транзакции (ограничение PostgreSQL).

Revision ID: 0023_jump_payout_requisites
Revises: 0022_tbank_ops_account
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_jump_payout_requisites"
down_revision = "0022_tbank_ops_account"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TYPE payment_provider ADD VALUE IF NOT EXISTS 'jump'")

    op.create_table(
        "payout_requisites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("sbp_phone_enc", postgresql.BYTEA(), nullable=False),
        sa.Column("sbp_bank_id", sa.Text(), nullable=False),
        sa.Column("last_name_enc", postgresql.BYTEA(), nullable=False),
        sa.Column("first_name_enc", postgresql.BYTEA(), nullable=False),
        sa.Column("middle_name_enc", postgresql.BYTEA(), nullable=True),
        sa.Column(
            "created_at", postgresql.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column(
            "updated_at", postgresql.TIMESTAMP(timezone=True), nullable=False
        ),
    )
    op.create_index(
        "ix_payout_requisites_user_id", "payout_requisites", ["user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_payout_requisites_user_id", table_name="payout_requisites")
    op.drop_table("payout_requisites")
    # Значение enum 'jump' не удаляем: PostgreSQL не поддерживает DROP VALUE.
