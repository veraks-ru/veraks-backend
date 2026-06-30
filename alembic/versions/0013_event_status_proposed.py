"""events: статус proposed (пользовательские предложения на модерацию)

Добавляет значение ``proposed`` к нативному enum ``event_status``.
``ALTER TYPE ... ADD VALUE`` не выполняется внутри транзакции — autocommit-блок.

Revision ID: 0013_event_status_proposed
Revises: 0012_subscription_plan_tariffs
Create Date: 2026-06-30
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_event_status_proposed"
down_revision: str | None = "0012_subscription_plan_tariffs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет значение enum вне транзакционного блока (требование Postgres)."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_status ADD VALUE IF NOT EXISTS 'proposed'")


def downgrade() -> None:
    """Postgres не умеет удалять значения enum напрямую — необратимо (no-op)."""
    pass
