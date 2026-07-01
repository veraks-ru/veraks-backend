"""b2b: таблица api_keys для signal API (ключи доступа B2B-потребителей)

Revision ID: 0018_create_api_keys
Revises: 0017_prize_fund_sponsor_user
Create Date: 2026-07-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0018_create_api_keys"
down_revision: str | None = "0017_prize_fund_sponsor_user"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "owner_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("key_prefix", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False, unique=True),
        sa.Column("daily_quota", sa.Integer(), nullable=False),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_api_keys_owner_user_id", "api_keys", ["owner_user_id"])
    op.create_index("ix_api_keys_is_active", "api_keys", ["is_active"])


def downgrade() -> None:
    op.drop_table("api_keys")
