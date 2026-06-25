"""audit_log: общий неизменяемый журнал с хеш-цепочкой

Вводит кросс-доменную инфраструктуру аудита (см. ``app/shared/audit``):
enum ``audit_actor_type``, таблицу ``audit_log`` (``bigserial`` id,
``before``/``after``/``metadata`` jsonb, звено ``prev_hash``/``hash``) и общую
функцию ``block_mutations()`` с триггером, запрещающим UPDATE/DELETE — схемная
гарантия append-only (в духе триггера раздельных касс из задания). Дополняет
правило «у роли приложения нет UPDATE/DELETE» на уровне самой схемы.

Функция ``block_mutations()`` создаётся здесь и переиспользуется миграцией
``0009`` для таблицы ``resolutions``.

Revision ID: 0008_create_audit_log
Revises: 0007_link_events_season_fk
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_create_audit_log"
down_revision: str | None = "0007_link_events_season_fk"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

audit_actor_type = postgresql.ENUM(
    "user",
    "editor",
    "arbiter",
    "admin",
    "system",
    name="audit_actor_type",
    create_type=False,
)

_BLOCK_MUTATIONS_FN = """
CREATE OR REPLACE FUNCTION block_mutations() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Table % is append-only: % is forbidden',
        TG_TABLE_NAME, TG_OP;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    """Создаёт enum, таблицу audit_log, функцию и триггер append-only."""
    bind = op.get_bind()
    audit_actor_type.create(bind, checkfirst=True)

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("actor_type", audit_actor_type, nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("before", postgresql.JSONB(), nullable=True),
        sa.Column("after", postgresql.JSONB(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("prev_hash", sa.Text(), nullable=True),
        sa.Column("hash", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"]
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_occurred_at", "audit_log", ["occurred_at"])

    # Append-only на уровне схемы: запрет UPDATE/DELETE триггером.
    op.execute(_BLOCK_MUTATIONS_FN)
    op.execute(
        "CREATE TRIGGER trg_audit_log_append_only "
        "BEFORE UPDATE OR DELETE ON audit_log "
        "FOR EACH ROW EXECUTE FUNCTION block_mutations();"
    )


def downgrade() -> None:
    """Снимает триггер/функцию, таблицу и enum."""
    op.execute("DROP TRIGGER IF EXISTS trg_audit_log_append_only ON audit_log;")
    op.drop_index("ix_audit_log_occurred_at", table_name="audit_log")
    op.drop_index("ix_audit_log_action", table_name="audit_log")
    op.drop_index("ix_audit_log_actor_id", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
    # Функция общая — удаляем после того, как 0009 снял свой триггер с resolutions.
    op.execute("DROP FUNCTION IF EXISTS block_mutations();")
    audit_actor_type.drop(op.get_bind(), checkfirst=True)
