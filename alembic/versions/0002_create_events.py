"""events: создание таблиц categories и events

Revision ID: 0002_create_events
Revises: 0001_create_users
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_create_events"
down_revision: str | None = "0001_create_users"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

event_status = postgresql.ENUM(
    "draft",
    "open",
    "closed",
    "resolving",
    "resolved",
    "cancelled",
    "disputed",
    name="event_status",
    create_type=False,
)


def upgrade() -> None:
    """enum статусов, таблицы categories и events с индексами."""
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    bind = op.get_bind()
    event_status.create(bind, checkfirst=True)

    # ── categories (дерево через self-FK) ──────────────────────────────────
    op.create_table(
        "categories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "parent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id"),
            nullable=True,
        ),
    )
    op.create_unique_constraint("uq_categories_slug", "categories", ["slug"])
    op.create_index("ix_categories_parent_id", "categories", ["parent_id"])

    # ── events ──────────────────────────────────────────────────────────────
    op.create_table(
        "events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "category_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("categories.id"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        # FK на seasons добавится вместе с доменом seasons. TODO(seasons).
        sa.Column("season_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", event_status, nullable=False, server_default="draft"),
        sa.Column("opens_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("closes_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolves_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("resolution_source", sa.Text(), nullable=False),
        sa.Column("resolution_criteria", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Boolean(), nullable=True),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "dispute_window_ends_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Инвариант временного окна на уровне схемы (дублирует доменный VO).
        sa.CheckConstraint("opens_at < closes_at", name="ck_events_window_order"),
        sa.CheckConstraint(
            "closes_at <= resolves_at", name="ck_events_resolves_after_close"
        ),
    )
    op.create_index("ix_events_status", "events", ["status"])
    op.create_index("ix_events_category_id", "events", ["category_id"])
    op.create_index("ix_events_closes_at", "events", ["closes_at"])
    op.create_index("ix_events_resolves_at", "events", ["resolves_at"])
    op.create_index("ix_events_season_id", "events", ["season_id"])


def downgrade() -> None:
    """Откат таблиц events/categories и enum статусов."""
    op.drop_index("ix_events_season_id", table_name="events")
    op.drop_index("ix_events_resolves_at", table_name="events")
    op.drop_index("ix_events_closes_at", table_name="events")
    op.drop_index("ix_events_category_id", table_name="events")
    op.drop_index("ix_events_status", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_categories_parent_id", table_name="categories")
    op.drop_table("categories")
    bind = op.get_bind()
    event_status.drop(bind, checkfirst=True)
