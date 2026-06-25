"""predictions: создание таблицы predictions

Revision ID: 0003_create_predictions
Revises: 0002_create_events
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_create_predictions"
down_revision: str | None = "0002_create_events"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

confidence_grade = postgresql.ENUM(
    "definitely_no",
    "probably_no",
    "fifty_fifty",
    "probably_yes",
    "definitely_yes",
    name="confidence_grade",
    create_type=False,
)


def upgrade() -> None:
    """enum градаций уверенности и таблица predictions с индексами."""
    bind = op.get_bind()
    confidence_grade.create(bind, checkfirst=True)

    op.create_table(
        "predictions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id"),
            nullable=False,
        ),
        sa.Column("confidence_grade", confidence_grade, nullable=False),
        sa.Column("probability", sa.Numeric(3, 2), nullable=False),
        sa.Column(
            "is_locked", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("brier_score", sa.Numeric(6, 5), nullable=True),
        sa.Column("scored_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        # Вероятность всегда корректна как доля (дублирует доменный инвариант).
        sa.CheckConstraint(
            "probability >= 0 AND probability <= 1",
            name="ck_predictions_probability_range",
        ),
    )

    # Ядро антифрода/честности: один прогноз на пользователя на событие.
    op.create_unique_constraint(
        "uq_predictions_user_event", "predictions", ["user_id", "event_id"]
    )
    op.create_index("ix_predictions_user_id", "predictions", ["user_id"])
    op.create_index("ix_predictions_event_id", "predictions", ["event_id"])
    # Для калибровки (predicted vs actual) по бинам вероятности.
    op.create_index(
        "ix_predictions_event_probability",
        "predictions",
        ["event_id", "probability"],
    )
    # Частичный индекс по заблокированным — горячая выборка для скоринга.
    op.create_index(
        "ix_predictions_event_locked",
        "predictions",
        ["event_id"],
        postgresql_where=sa.text("is_locked"),
    )


def downgrade() -> None:
    """Откат таблицы predictions и enum градаций."""
    op.drop_index("ix_predictions_event_locked", table_name="predictions")
    op.drop_index("ix_predictions_event_probability", table_name="predictions")
    op.drop_index("ix_predictions_event_id", table_name="predictions")
    op.drop_index("ix_predictions_user_id", table_name="predictions")
    op.drop_table("predictions")
    bind = op.get_bind()
    confidence_grade.drop(bind, checkfirst=True)
