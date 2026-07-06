"""resolutions: частичные UNIQUE-индексы против гонок статуса события

Два инварианта на уровне БД (страховка поверх прикладной блокировки
``SELECT … FOR UPDATE`` в use-cases resolutions):

* ``uq_disputes_one_open_per_event`` — не более ОДНОГО открытого спора
  (``status IN ('open','under_review')``) на событие. Закрывает гонку двух
  конкурентных ``RaiseDispute``: без него обе транзакции могли вставить по
  открытому спору на одно событие (застрявший спор, вечная блокировка
  скоринга/финализации сезона). Заменяет собой неуникальный частичный индекс
  ``ix_disputes_open_by_event`` из 0009 (тот же предикат и колонка — уникальный
  индекс обслуживает и «горячий» запрос «есть ли открытый спор»).

* ``uq_resolutions_one_root_final_per_event`` — не более ОДНОЙ корневой
  ``final``-резолюции (без ``supersedes_id``) на событие. Закрывает гонку двух
  конкурентных ``FixResolution``: исходную фиксацию исхода пишет ровно одна
  резолюция без предшественника; пересмотры (overturn) имеют
  ``supersedes_id IS NOT NULL`` и под предикат не попадают, поэтому цепочка
  пересмотров остаётся допустимой.

Revision ID: 0020_resolution_race_guards
Revises: 0019_ledger_payout_guards
Create Date: 2026-07-05
"""
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0020_resolution_race_guards"
down_revision: str | None = "0019_ledger_payout_guards"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Ставит частичные UNIQUE-индексы; заменяет неуникальный индекс споров."""
    # Один открытый спор на событие. Уникальный индекс полностью покрывает
    # запрос, ради которого в 0009 заводился неуникальный ix_disputes_open_by_event,
    # поэтому старый индекс снимаем во избежание дублирования.
    op.execute("DROP INDEX IF EXISTS ix_disputes_open_by_event")
    op.execute(
        "CREATE UNIQUE INDEX uq_disputes_one_open_per_event "
        "ON disputes (event_id) "
        "WHERE status IN ('open', 'under_review')"
    )

    # Одна корневая final-резолюция (без supersedes) на событие.
    op.execute(
        "CREATE UNIQUE INDEX uq_resolutions_one_root_final_per_event "
        "ON resolutions (event_id) "
        "WHERE status = 'final' AND supersedes_id IS NULL"
    )


def downgrade() -> None:
    """Снимает UNIQUE-индексы и восстанавливает неуникальный индекс из 0009."""
    op.execute("DROP INDEX IF EXISTS uq_resolutions_one_root_final_per_event")
    op.execute("DROP INDEX IF EXISTS uq_disputes_one_open_per_event")
    op.execute(
        "CREATE INDEX ix_disputes_open_by_event "
        "ON disputes (event_id) "
        "WHERE status IN ('open', 'under_review')"
    )
