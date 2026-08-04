"""events: статус annulled (аннулирование события после резолюции)

Организатор конкурса вправе признать событие некорректным уже ПОСЛЕ фиксации
исхода (ст. 1058 ГК РФ, PRD §7.5/§4.8): двусмысленная формулировка, ошибка
источника, неразрешимый спор. Такое событие целиком исключается из рейтингов
и калибровки. Не путать с ``cancelled`` — отменой ДО подведения исхода.

Добавляет значение ``annulled`` к нативному enum ``event_status``.
``ALTER TYPE ... ADD VALUE`` не выполняется внутри транзакции — autocommit-блок.

Revision ID: 0026_event_status_annulled
Revises: 0025_consents_onboarding
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0026_event_status_annulled"
down_revision: str | None = "0025_consents_onboarding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет значение enum вне транзакционного блока (требование Postgres)."""
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE event_status ADD VALUE IF NOT EXISTS 'annulled'")


def downgrade() -> None:
    """Postgres не умеет удалять значения enum напрямую — необратимо (no-op)."""
    pass
