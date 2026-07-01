"""leagues: приватные лиги и дивизионы (лестница уровней) + сид дивизионов

Revision ID: 0016_create_leagues
Revises: 0015_create_social
Create Date: 2026-07-01
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016_create_leagues"
down_revision: str | None = "0015_create_social"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Стартовая лестница дивизионов (1 = высший).
_DIVISIONS = [
    (1, "Высший дивизион"),
    (2, "Первый дивизион"),
    (3, "Второй дивизион"),
]


def upgrade() -> None:
    op.create_table(
        "leagues",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "owner_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("invite_code", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_leagues_owner_id", "leagues", ["owner_id"])

    op.create_table(
        "league_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "league_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("leagues.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("joined_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("league_id", "user_id", name="uq_league_member"),
    )
    op.create_index(
        "ix_league_memberships_league_id", "league_memberships", ["league_id"]
    )
    op.create_index(
        "ix_league_memberships_user_id", "league_memberships", ["user_id"]
    )

    divisions = op.create_table(
        "divisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("level", sa.Integer(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=False),
    )

    op.create_table(
        "division_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "season_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("seasons.id"),
            nullable=False,
        ),
        sa.Column(
            "division_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("divisions.id"),
            nullable=False,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "season_id", name="uq_division_member"),
    )
    op.create_index(
        "ix_division_memberships_season_id", "division_memberships", ["season_id"]
    )
    op.create_index(
        "ix_division_memberships_division_id",
        "division_memberships",
        ["division_id"],
    )
    op.create_index(
        "ix_division_memberships_user_id", "division_memberships", ["user_id"]
    )

    op.bulk_insert(
        divisions,
        [
            {"id": uuid.uuid4(), "level": level, "title": title}
            for level, title in _DIVISIONS
        ],
    )


def downgrade() -> None:
    op.drop_table("division_memberships")
    op.drop_table("divisions")
    op.drop_table("league_memberships")
    op.drop_table("leagues")
