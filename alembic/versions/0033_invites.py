"""billing: пригласительные ссылки и выданный по ним доступ

Голосовать можно с активной подпиской. Приглашение открывает ту же
возможность без оплаты: на старте платформе нужны участники, а первым из них
платить не за что — прогнозов ещё нет, рейтинга тоже.

Отдельные таблицы, а не подписка с нулевой ценой: подписка — платёжная
сущность с положительной ценой, каждое списание отражается проводкой в кассе
OPERATIONS. У выданного доступа нет ни платежа, ни проводки, и заводить ради
него запись в денежном контуре нельзя — это исказило бы отчётность по кассе.

Одноразовость приглашения держится на ``UNIQUE(invite_id)`` в
``access_grants``: две одновременные активации одной ссылки дадут гонку, и
проигравшая транзакция получит нарушение уникальности, а не второй доступ.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033_invites"
down_revision: str | None = "0032_event_public_code"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "invites",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "code",
            sa.Text(),
            nullable=False,
            comment="Код в ссылке /join?invite=… (11 символов base64url).",
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "duration_days",
            sa.Integer(),
            nullable=True,
            comment="Срок доступа от момента активации; NULL — бессрочно.",
        ),
        sa.Column("note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "redeemed_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column("redeemed_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("revoked_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_days IS NULL OR duration_days > 0",
            name="ck_invites_duration_positive",
        ),
    )
    op.create_index("ix_invites_code", "invites", ["code"], unique=True)
    op.create_index("ix_invites_created_by", "invites", ["created_by"])

    op.create_table(
        "access_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "invite_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("invites.id"),
            nullable=False,
        ),
        sa.Column(
            "expires_at",
            postgresql.TIMESTAMP(timezone=True),
            nullable=True,
            comment="Когда доступ закончится; NULL — бессрочно.",
        ),
        sa.Column("granted_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_access_grants_user_id", "access_grants", ["user_id"])
    # Одно приглашение — ровно один доступ: это и есть одноразовость.
    op.create_index(
        "ix_access_grants_invite_id", "access_grants", ["invite_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_access_grants_invite_id", table_name="access_grants")
    op.drop_index("ix_access_grants_user_id", table_name="access_grants")
    op.drop_table("access_grants")
    op.drop_index("ix_invites_created_by", table_name="invites")
    op.drop_index("ix_invites_code", table_name="invites")
    op.drop_table("invites")
