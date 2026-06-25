"""scoring: создание таблицы ratings (материализованные рейтинги)

Revision ID: 0004_create_ratings
Revises: 0003_create_predictions
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_create_ratings"
down_revision: str | None = "0003_create_predictions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

rating_scope = postgresql.ENUM(
    "global",
    "category",
    "season",
    name="rating_scope",
    create_type=False,
)

# Сентинел для уникальности при ``scope_id IS NULL`` (область global): в обычном
# UNIQUE Postgres считает NULL'ы различными, поэтому ключ строим через COALESCE.
_NULL_SCOPE_SENTINEL = "00000000-0000-0000-0000-000000000000"


def upgrade() -> None:
    """enum областей рейтинга и таблица ratings с индексами."""
    bind = op.get_bind()
    rating_scope.create(bind, checkfirst=True)

    op.create_table(
        "ratings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("scope_type", rating_scope, nullable=False),
        # category_id / season_id; NULL для global.
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("mean_brier", sa.Numeric(6, 5), nullable=False),
        sa.Column("skill_score", sa.Numeric(6, 5), nullable=False),
        sa.Column("calibration_error", sa.Numeric(6, 5), nullable=False),
        sa.Column("n_resolved", sa.Integer(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # Метрики-доли корректны (дублирует доменные инварианты).
        sa.CheckConstraint(
            "mean_brier >= 0 AND mean_brier <= 1",
            name="ck_ratings_mean_brier_range",
        ),
        sa.CheckConstraint(
            "calibration_error >= 0 AND calibration_error <= 1",
            name="ck_ratings_calibration_error_range",
        ),
        sa.CheckConstraint("n_resolved >= 0", name="ck_ratings_n_resolved_nonneg"),
    )

    op.create_index("ix_ratings_user_id", "ratings", ["user_id"])
    # Горячее чтение топа области.
    op.create_index(
        "ix_ratings_scope_rank", "ratings", ["scope_type", "scope_id", "rank"]
    )
    # Альтернативная сортировка/аналитика по среднему Brier.
    op.create_index(
        "ix_ratings_scope_mean_brier",
        "ratings",
        ["scope_type", "scope_id", "mean_brier"],
    )
    # Один рейтинг на (пользователь, область): COALESCE учитывает NULL у global.
    op.execute(
        "CREATE UNIQUE INDEX uq_ratings_user_scope ON ratings "
        f"(user_id, scope_type, COALESCE(scope_id, '{_NULL_SCOPE_SENTINEL}'::uuid))"
    )


def downgrade() -> None:
    """Откат таблицы ratings и enum областей."""
    op.execute("DROP INDEX IF EXISTS uq_ratings_user_scope")
    op.drop_index("ix_ratings_scope_mean_brier", table_name="ratings")
    op.drop_index("ix_ratings_scope_rank", table_name="ratings")
    op.drop_index("ix_ratings_user_id", table_name="ratings")
    op.drop_table("ratings")
    bind = op.get_bind()
    rating_scope.drop(bind, checkfirst=True)
