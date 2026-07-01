"""billing: владелец фонда (prize_funds.sponsor_user_id) для кабинета спонсора

Revision ID: 0017_prize_fund_sponsor_user
Revises: 0016_create_leagues
Create Date: 2026-07-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0017_prize_fund_sponsor_user"
down_revision: str | None = "0016_create_leagues"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "prize_funds",
        sa.Column(
            "sponsor_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_prize_funds_sponsor_user_id", "prize_funds", ["sponsor_user_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_prize_funds_sponsor_user_id", table_name="prize_funds")
    op.drop_column("prize_funds", "sponsor_user_id")
