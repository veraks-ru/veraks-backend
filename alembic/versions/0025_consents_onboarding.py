"""identity: таблица user_consents + флаг онбординга (152-ФЗ, T2)

При первом входе нужно фиксировать факт принятия оферты и согласия на
обработку ПДн — с версией документа, датой и способом (юр. блокер до этой
миграции: согласия не хранились вообще). Заодно вводим ``users.onboarded_at``
— отметку прохождения онбординга (выбор псевдонима + согласия); у
существующих пользователей она остаётся ``NULL`` — они пройдут онбординг
при следующем входе (юридически корректно: задним числом согласие не
получить).

``user_consents`` — append-only (как ``resolutions``/``ledger_*``): тот же
``block_mutations()`` из 0008 запрещает UPDATE/DELETE, правки — только
новыми строками. ``UNIQUE(user_id, document, version)`` делает повторное
принятие одной и той же версии идемпотентным на уровне схемы.

Revision ID: 0025_consents_onboarding
Revises: 0024_hash_esia_oid
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0025_consents_onboarding"
down_revision: str | None = "0024_hash_esia_oid"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Добавляет users.onboarded_at и таблицу user_consents (append-only)."""
    op.add_column(
        "users", sa.Column("onboarded_at", sa.TIMESTAMP(timezone=True), nullable=True)
    )

    op.create_table(
        "user_consents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("version", sa.Text(), nullable=False),
        sa.Column(
            "accepted_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("method", sa.Text(), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
    )
    op.create_index("ix_user_consents_user_id", "user_consents", ["user_id"])
    op.create_unique_constraint(
        "uq_user_consents_user_document_version",
        "user_consents",
        ["user_id", "document", "version"],
    )
    # Append-only: правки только новыми строками (функция из 0008).
    op.execute(
        "CREATE TRIGGER trg_user_consents_append_only "
        "BEFORE UPDATE OR DELETE ON user_consents "
        "FOR EACH ROW EXECUTE FUNCTION block_mutations();"
    )


def downgrade() -> None:
    """Откатывает таблицу user_consents и колонку users.onboarded_at."""
    op.execute("DROP TRIGGER IF EXISTS trg_user_consents_append_only ON user_consents")
    op.drop_table("user_consents")
    op.drop_column("users", "onboarded_at")
