"""social: комментарии к событиям и подписки на предсказателей

Revision ID: 0015_create_social
Revises: 0014_create_notifications
Create Date: 2026-07-01
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015_create_social"
down_revision: str | None = "0014_create_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "comments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id"),
            nullable=False,
        ),
        sa.Column(
            "author_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_comments_event_id", "comments", ["event_id"])
    op.create_index("ix_comments_author_id", "comments", ["author_id"])

    op.create_table(
        "follows",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "follower_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "followee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "follower_id", "followee_id", name="uq_follows_pair"
        ),
    )
    op.create_index("ix_follows_follower_id", "follows", ["follower_id"])
    op.create_index("ix_follows_followee_id", "follows", ["followee_id"])


def downgrade() -> None:
    op.drop_table("follows")
    op.drop_table("comments")
