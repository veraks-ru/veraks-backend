"""resolutions: статус спора voided (снят вместе с аннулированием события)

Аннулирование ``disputed``-события (0026) оставляло открытый спор навсегда:
решить его нельзя — обе ветки решения арбитра ведут через запрещённый переход
``annulled → resolved``, а незакрытый спор вечно блокировал бы финализацию
сезона. ``voided`` — терминальный статус «спор снят, предмета спора нет».

``ALTER TYPE ... ADD VALUE`` не выполняется внутри транзакции — autocommit-блок.

Revision ID: 0027_dispute_status_voided
Revises: 0026_event_status_annulled
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0027_dispute_status_voided"
down_revision: str | None = "0026_event_status_annulled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет значение enum вне транзакционного блока (требование Postgres)."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE dispute_status ADD VALUE IF NOT EXISTS 'voided'")


def downgrade() -> None:
    """Postgres не умеет удалять значения enum напрямую — необратимо (no-op)."""
    pass
