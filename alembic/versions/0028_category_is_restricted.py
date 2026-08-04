"""events: категории — флаг запрещённой тематики ``is_restricted``

PRD §7.5: события в категориях с ``is_restricted=true`` (смерть/здоровье
конкретных лиц, насилие, теракты, экстремизм, частная жизнь) не создаются и
не предлагаются — проверка на уровне ``CreateEvent``/``ProposeEvent``.
Backfill не нужен: старые категории по умолчанию не запрещены.

Revision ID: 0028_category_is_restricted
Revises: 0027_dispute_status_voided
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028_category_is_restricted"
down_revision: str | None = "0027_dispute_status_voided"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет ``categories.is_restricted`` (``NOT NULL DEFAULT false``)."""
    op.add_column(
        "categories",
        sa.Column(
            "is_restricted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    """Удаляет колонку ``is_restricted``."""
    op.drop_column("categories", "is_restricted")
