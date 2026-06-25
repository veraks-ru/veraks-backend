"""resolutions/disputes: журнал решений, споры и маркеры скоринга

Создаёт домен resolutions: enum'ы ``resolution_status`` и ``dispute_status``;
append-only журнал ``resolutions`` (self-FK ``supersedes_id`` для пересмотров,
триггер append-only через общую ``block_mutations()`` из 0008); изменяемую
таблицу ``disputes`` (жизненный цикл оспаривания) с частичным индексом по
открытым спорам; служебную ``resolution_scoring_dispatches`` (идемпотентность
постановки скоринга по резолюции).

Revision ID: 0009_create_resolutions_disputes
Revises: 0008_create_audit_log
Create Date: 2026-06-25
"""
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_create_resolutions_disputes"
down_revision: str | None = "0008_create_audit_log"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

resolution_status = postgresql.ENUM(
    "proposed",
    "final",
    "overturned",
    name="resolution_status",
    create_type=False,
)
dispute_status = postgresql.ENUM(
    "open",
    "under_review",
    "accepted",
    "rejected",
    name="dispute_status",
    create_type=False,
)


def upgrade() -> None:
    """Создаёт enum'ы и таблицы resolutions/disputes/dispatches."""
    bind = op.get_bind()
    resolution_status.create(bind, checkfirst=True)
    dispute_status.create(bind, checkfirst=True)

    # ── resolutions (append-only журнал решений) ──────────────────────────
    op.create_table(
        "resolutions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id"),
            nullable=False,
        ),
        sa.Column("outcome", sa.Boolean(), nullable=False),
        sa.Column("status", resolution_status, nullable=False),
        sa.Column(
            "resolved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column(
            "supersedes_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("resolutions.id"),
            nullable=True,
        ),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_resolutions_event_id", "resolutions", ["event_id"])
    op.create_index(
        "ix_resolutions_event_status", "resolutions", ["event_id", "status"]
    )

    # Append-only на уровне схемы (функция block_mutations() создана в 0008).
    op.execute(
        "CREATE TRIGGER trg_resolutions_append_only "
        "BEFORE UPDATE OR DELETE ON resolutions "
        "FOR EACH ROW EXECUTE FUNCTION block_mutations();"
    )

    # ── disputes (изменяемый жизненный цикл) ──────────────────────────────
    op.create_table(
        "disputes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id"),
            nullable=False,
        ),
        sa.Column(
            "resolution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("resolutions.id"),
            nullable=False,
        ),
        sa.Column(
            "raised_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", dispute_status, nullable=False),
        sa.Column(
            "decided_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "decision_notes", sa.Text(), nullable=False, server_default=""
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("decided_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_disputes_event_id", "disputes", ["event_id"])
    op.create_index("ix_disputes_status", "disputes", ["status"])
    op.create_index("ix_disputes_raised_by", "disputes", ["raised_by"])
    # Частичный индекс под горячий запрос «есть ли открытый спор».
    op.create_index(
        "ix_disputes_open_by_event",
        "disputes",
        ["event_id"],
        postgresql_where=sa.text("status IN ('open', 'under_review')"),
    )

    # ── resolution_scoring_dispatches (маркеры скоринга) ──────────────────
    op.create_table(
        "resolution_scoring_dispatches",
        sa.Column(
            "resolution_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("resolutions.id"),
            primary_key=True,
        ),
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("events.id"),
            nullable=False,
        ),
        sa.Column("dispatched_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )


def downgrade() -> None:
    """Снимает таблицы/триггер/enum'ы домена resolutions."""
    op.drop_table("resolution_scoring_dispatches")
    op.drop_index("ix_disputes_open_by_event", table_name="disputes")
    op.drop_index("ix_disputes_raised_by", table_name="disputes")
    op.drop_index("ix_disputes_status", table_name="disputes")
    op.drop_index("ix_disputes_event_id", table_name="disputes")
    op.drop_table("disputes")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_resolutions_append_only ON resolutions;"
    )
    op.drop_index("ix_resolutions_event_status", table_name="resolutions")
    op.drop_index("ix_resolutions_event_id", table_name="resolutions")
    op.drop_table("resolutions")
    bind = op.get_bind()
    dispute_status.drop(bind, checkfirst=True)
    resolution_status.drop(bind, checkfirst=True)
