"""scoring: добавление ratings.qualified (флаг квалификации сезона)

Колонка имеет смысл только для сезонной области рейтинга: ``true``/``false`` —
прошёл ли пользователь пороги квалификации к призам; ``NULL`` — для
global/category (неприменимо) и для сезона, чьи правила недоступны при
пересчёте. Nullable, без backfill (старые строки = NULL, что корректно).

Revision ID: 0006_add_ratings_qualified
Revises: 0005_create_seasons
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_ratings_qualified"
down_revision: str | None = "0005_create_seasons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет nullable-колонку ``qualified`` в ``ratings``."""
    op.add_column(
        "ratings",
        sa.Column("qualified", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    """Удаляет колонку ``qualified``."""
    op.drop_column("ratings", "qualified")
