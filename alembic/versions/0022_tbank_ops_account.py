"""Счёт операционной кассы ТБанк + защита от двойного возврата.

Добавляет счёт ``ops:cash:tbank`` (касса OPERATIONS) для приёма платежей за
подписку через ТБанк и частичный UNIQUE на ``external_ref`` проводок вида
``refund`` — гарантия идемпотентности возврата (по образцу prize_payout guard,
миграция 0019). Вид проводки ``refund`` уже есть в enum ``transaction_kind``.

Revision ID: 0022_tbank_ops_account
Revises: 0021_finalizations_append_only
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_tbank_ops_account"
down_revision = "0021_finalizations_append_only"
branch_labels = None
depends_on = None

# Ссылка на существующий enum кассы (create_type=False — тип уже создан в 0010).
_LEDGER_TYPE = postgresql.ENUM(
    "operations", "prize", name="ledger_type", create_type=False
)


def upgrade() -> None:
    accounts = sa.table(
        "ledger_accounts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("ledger_type", _LEDGER_TYPE),
        sa.column("account_code", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("currency", sa.Text()),
    )
    op.bulk_insert(
        accounts,
        [
            {
                "id": uuid.uuid4(),
                "ledger_type": "operations",
                "account_code": "ops:cash:tbank",
                "title": "Операционный кэш ТБанк",
                "currency": "RUB",
            }
        ],
    )
    # Идемпотентность возврата: один external_ref возврата = одна проводка.
    op.create_index(
        "uq_ledger_txn_refund_ref",
        "ledger_transactions",
        ["external_ref"],
        unique=True,
        postgresql_where=sa.text("kind = 'refund'"),
    )


def downgrade() -> None:
    op.drop_index("uq_ledger_txn_refund_ref", table_name="ledger_transactions")
    op.execute("DELETE FROM ledger_accounts WHERE account_code = 'ops:cash:tbank'")
