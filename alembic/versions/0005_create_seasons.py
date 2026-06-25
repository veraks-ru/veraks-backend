"""seasons: создание таблиц seasons и журнала финализаций

Создаёт только домен seasons: enum статусов, таблицу ``seasons`` (slug citext
UNIQUE, league_config jsonb NULL, индекс по статусу) и append-only журнал
финализаций (родитель ``season_finalizations`` + строки-на-участника
``season_finalization_entries``). **Не** трогает ``events`` — отложенный FK
``events.season_id → seasons.id`` выносится в отдельную миграцию 0007 (после
верификации/бэкфилла существующих «голых» season_id).

Revision ID: 0005_create_seasons
Revises: 0004_create_ratings
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_create_seasons"
down_revision: str | None = "0004_create_ratings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

season_status = postgresql.ENUM(
    "upcoming",
    "active",
    "finished",
    name="season_status",
    create_type=False,
)


def upgrade() -> None:
    """enum статусов сезона, таблица seasons и append-only журнал финализаций."""
    # Расширение citext уже создано миграцией events (0002); подстрахуемся.
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    bind = op.get_bind()
    season_status.create(bind, checkfirst=True)

    # ── seasons ───────────────────────────────────────────────────────────────
    op.create_table(
        "seasons",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", postgresql.CITEXT(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("ends_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("status", season_status, nullable=False),
        # Снапшот LeagueConfig; NULL пока сезон не активирован.
        sa.Column("league_config", postgresql.JSONB(), nullable=True),
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
        # Инвариант окна сезона на уровне схемы (дублирует доменную проверку).
        sa.CheckConstraint("starts_at < ends_at", name="ck_seasons_window_order"),
    )
    op.create_unique_constraint("uq_seasons_slug", "seasons", ["slug"])
    op.create_index("ix_seasons_status", "seasons", ["status"])

    # ── season_finalizations (append-only родитель) ─────────────────────────────
    op.create_table(
        "season_finalizations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "season_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("seasons.id"),
            nullable=False,
        ),
        sa.Column("finalized_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("league_config", postgresql.JSONB(), nullable=False),
        sa.Column("qualified_count", sa.Integer(), nullable=False),
        sa.Column("total_participants", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_season_finalizations_season_id", "season_finalizations", ["season_id"]
    )

    # ── season_finalization_entries (append-only строки-на-участника) ───────────
    op.create_table(
        "season_finalization_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "finalization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("season_finalizations.id"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column("skill_score", sa.Numeric(6, 5), nullable=False),
        sa.Column("mean_brier", sa.Numeric(6, 5), nullable=False),
        sa.Column("calibration_error", sa.Numeric(6, 5), nullable=False),
        sa.Column("n_resolved", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_season_finalization_entries_finalization_rank",
        "season_finalization_entries",
        ["finalization_id", "rank"],
    )

    # TODO(audit-infra): отозвать UPDATE/DELETE на season_finalizations и
    # season_finalization_entries у роли приложения (append-only, как resolutions
    # и ledger_*). Делается отдельным инфра-шагом управления ролями БД.


def downgrade() -> None:
    """Откат таблиц seasons/финализаций и enum статусов."""
    op.drop_index(
        "ix_season_finalization_entries_finalization_rank",
        table_name="season_finalization_entries",
    )
    op.drop_table("season_finalization_entries")
    op.drop_index(
        "ix_season_finalizations_season_id", table_name="season_finalizations"
    )
    op.drop_table("season_finalizations")
    op.drop_index("ix_seasons_status", table_name="seasons")
    op.drop_constraint("uq_seasons_slug", "seasons", type_="unique")
    op.drop_table("seasons")
    bind = op.get_bind()
    season_status.drop(bind, checkfirst=True)
