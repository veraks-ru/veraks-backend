"""billing: тарифы подписки daily/weekly (расширение enum subscription_plan)

Добавляет значения ``daily`` и ``weekly`` к нативному enum ``subscription_plan``
(были только ``monthly``/``annual``). ``ALTER TYPE ... ADD VALUE`` не выполняется
внутри транзакции — используем autocommit-блок.

Revision ID: 0012_subscription_plan_tariffs
Revises: 0011_revoke_append_only_grants
Create Date: 2026-06-30
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0012_subscription_plan_tariffs"
down_revision: str | None = "0011_revoke_append_only_grants"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет значения enum вне транзакционного блока (требование Postgres)."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE subscription_plan ADD VALUE IF NOT EXISTS 'daily'")
        op.execute("ALTER TYPE subscription_plan ADD VALUE IF NOT EXISTS 'weekly'")


def downgrade() -> None:
    """Postgres не умеет удалять значения enum напрямую — необратимо (no-op)."""
    pass
