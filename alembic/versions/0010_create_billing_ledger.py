"""billing: две кассы (ledger), подписки/платежи, призовой фонд и выплаты

Создаёт домен billing:

* план счетов ``ledger_accounts`` (с ``ledger_type`` — линия раздела касс),
  append-only журнал ``ledger_transactions`` и ``ledger_entries`` (двойная
  запись);
* ``subscriptions``/``payments`` (операционная касса), ``prize_funds``/
  ``payouts`` (призовая касса, maker-checker через ``created_by``/``approved_by``).

Схемные гарантии (зеркало доменных инвариантов):

* ``enforce_ledger_separation`` (BEFORE INSERT) — нога не может указывать на
  счёт чужой кассы: перетекание между OPERATIONS и PRIZE структурно невозможно;
* ``enforce_transaction_balanced`` (CONSTRAINT TRIGGER, DEFERRABLE INITIALLY
  DEFERRED) — на коммите внутри каждой транзакции сумма дебетов = сумма кредитов;
* ``block_mutations()`` из ``0008`` на ``ledger_transactions``/``ledger_entries``
  — append-only (нет UPDATE/DELETE);
* CHECK ``amount_kopecks > 0`` на ногах.

Засевается стандартный план счетов; счета конкретных фондов
(``prize:fund:<id>``) создаются в рантайме use-case'ом заведения фонда.

Revision ID: 0010_create_billing_ledger
Revises: 0009_create_resolutions_disputes
Create Date: 2026-06-25
"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_create_billing_ledger"
down_revision: str | None = "0009_create_resolutions_disputes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


ledger_type = postgresql.ENUM(
    "operations", "prize", name="ledger_type", create_type=False
)
entry_direction = postgresql.ENUM(
    "debit", "credit", name="entry_direction", create_type=False
)
transaction_kind = postgresql.ENUM(
    "subscription_payment",
    "b2b_invoice",
    "provider_fee",
    "refund",
    "sponsor_deposit",
    "prize_payout",
    "prize_tax",
    name="transaction_kind",
    create_type=False,
)
subscription_plan = postgresql.ENUM(
    "monthly", "annual", name="subscription_plan", create_type=False
)
subscription_status = postgresql.ENUM(
    "incomplete",
    "active",
    "past_due",
    "canceled",
    "expired",
    name="subscription_status",
    create_type=False,
)
payment_provider = postgresql.ENUM(
    "yookassa", "tbank", name="payment_provider", create_type=False
)
payment_status = postgresql.ENUM(
    "pending",
    "succeeded",
    "canceled",
    "refunded",
    name="payment_status",
    create_type=False,
)
payment_purpose = postgresql.ENUM(
    "subscription", "b2b", name="payment_purpose", create_type=False
)
prize_fund_status = postgresql.ENUM(
    "announced",
    "funded",
    "distributing",
    "closed",
    name="prize_fund_status",
    create_type=False,
)
payout_status = postgresql.ENUM(
    "pending",
    "approved",
    "processing",
    "paid",
    "failed",
    name="payout_status",
    create_type=False,
)

_ALL_ENUMS = [
    ledger_type,
    entry_direction,
    transaction_kind,
    subscription_plan,
    subscription_status,
    payment_provider,
    payment_status,
    payment_purpose,
    prize_fund_status,
    payout_status,
]

# Стандартный план счетов (счета фондов создаются в рантайме).
_SEED_ACCOUNTS: list[tuple[str, str, str]] = [
    ("operations", "ops:cash:yookassa", "Операционный кэш ЮKassa"),
    ("operations", "ops:revenue:subscriptions", "Выручка: подписки"),
    ("operations", "ops:revenue:b2b", "Выручка: B2B-сигнал"),
    ("operations", "ops:fee:provider", "Комиссия провайдера"),
    ("prize", "prize:cash:sponsor", "Призовой кэш спонсора"),
    ("prize", "prize:payable:winners", "К выплате победителям"),
    ("prize", "prize:tax:withheld", "Удержанный НДФЛ"),
]

_ENFORCE_SEPARATION_FN = """
CREATE OR REPLACE FUNCTION enforce_ledger_separation() RETURNS trigger AS $$
DECLARE acc_type ledger_type; txn_type ledger_type;
BEGIN
    SELECT ledger_type INTO acc_type FROM ledger_accounts     WHERE id = NEW.account_id;
    SELECT ledger_type INTO txn_type FROM ledger_transactions WHERE id = NEW.transaction_id;
    IF acc_type <> txn_type THEN
        RAISE EXCEPTION 'Cross-ledger entry forbidden: account % is %, transaction is %',
            NEW.account_id, acc_type, txn_type;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

_ENFORCE_BALANCED_FN = """
CREATE OR REPLACE FUNCTION enforce_transaction_balanced() RETURNS trigger AS $$
DECLARE d bigint; c bigint;
BEGIN
    SELECT
        COALESCE(SUM(amount_kopecks) FILTER (WHERE direction = 'debit'), 0),
        COALESCE(SUM(amount_kopecks) FILTER (WHERE direction = 'credit'), 0)
    INTO d, c
    FROM ledger_entries WHERE transaction_id = NEW.transaction_id;
    IF d <> c THEN
        RAISE EXCEPTION 'Unbalanced ledger transaction %: debit % <> credit %',
            NEW.transaction_id, d, c;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    """Создаёт enum'ы, таблицы, индексы, триггеры и засевает план счетов."""
    bind = op.get_bind()
    for enum in _ALL_ENUMS:
        enum.create(bind, checkfirst=True)

    # ── Леджер ────────────────────────────────────────────────────────────
    op.create_table(
        "ledger_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ledger_type", ledger_type, nullable=False),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "currency", sa.Text(), nullable=False, server_default=sa.text("'RUB'")
        ),
        sa.UniqueConstraint(
            "ledger_type", "account_code", name="uq_ledger_accounts_type_code"
        ),
    )
    op.create_index("ix_ledger_accounts_type", "ledger_accounts", ["ledger_type"])

    op.create_table(
        "ledger_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ledger_type", ledger_type, nullable=False),
        sa.Column("kind", transaction_kind, nullable=False),
        sa.Column("external_ref", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_ledger_transactions_type", "ledger_transactions", ["ledger_type"]
    )
    op.create_index("ix_ledger_transactions_kind", "ledger_transactions", ["kind"])
    op.create_index(
        "ix_ledger_transactions_external_ref",
        "ledger_transactions",
        ["external_ref"],
    )

    op.create_table(
        "ledger_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_accounts.id"),
            nullable=False,
        ),
        sa.Column("direction", entry_direction, nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.CheckConstraint("amount_kopecks > 0", name="ck_ledger_entries_amount_pos"),
    )
    op.create_index(
        "ix_ledger_entries_transaction_id", "ledger_entries", ["transaction_id"]
    )
    op.create_index("ix_ledger_entries_account_id", "ledger_entries", ["account_id"])
    op.create_index("ix_ledger_entries_created_at", "ledger_entries", ["created_at"])

    # ── Подписки / платежи (операционная касса) ───────────────────────────
    op.create_table(
        "subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column("plan", subscription_plan, nullable=False),
        sa.Column("price_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("provider", payment_provider, nullable=False),
        sa.Column("status", subscription_status, nullable=False),
        sa.Column("provider_subscription_id", sa.Text(), nullable=True),
        sa.Column("current_period_start", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("canceled_at", sa.TIMESTAMP(timezone=True), nullable=True),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_status", "subscriptions", ["status"])
    op.create_index(
        "ix_subscriptions_provider_sub_id",
        "subscriptions",
        ["provider_subscription_id"],
    )

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "subscription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("subscriptions.id"),
            nullable=True,
        ),
        sa.Column("provider", payment_provider, nullable=False),
        sa.Column("provider_payment_id", sa.Text(), nullable=False),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column("purpose", payment_purpose, nullable=False),
        sa.Column("status", payment_status, nullable=False),
        sa.Column("fiscal_receipt_id", sa.Text(), nullable=True),
        sa.Column(
            "ledger_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id"),
            nullable=True,
        ),
        sa.Column("paid_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "provider", "provider_payment_id", name="uq_payments_provider_ref"
        ),
    )
    op.create_index("ix_payments_user_id", "payments", ["user_id"])
    op.create_index("ix_payments_status", "payments", ["status"])

    # ── Призовой фонд / выплаты (призовая касса) ───────────────────────────
    op.create_table(
        "prize_funds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "season_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("seasons.id"),
            nullable=True,
        ),
        sa.Column("sponsor_name", sa.Text(), nullable=False),
        sa.Column("sponsor_ref", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("committed_kopecks", sa.BigInteger(), nullable=False),
        sa.Column(
            "deposited_kopecks",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "ledger_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_accounts.id"),
            nullable=False,
        ),
        sa.Column("status", prize_fund_status, nullable=False),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
    )
    op.create_index("ix_prize_funds_season_id", "prize_funds", ["season_id"])
    op.create_index("ix_prize_funds_status", "prize_funds", ["status"])

    op.create_table(
        "payouts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "prize_fund_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("prize_funds.id"),
            nullable=False,
        ),
        sa.Column(
            "season_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("seasons.id"),
            nullable=True,
        ),
        sa.Column("amount_kopecks", sa.BigInteger(), nullable=False),
        sa.Column(
            "tax_withheld_kopecks",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("status", payout_status, nullable=False),
        sa.Column("provider", payment_provider, nullable=True),
        sa.Column("provider_payout_id", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=False,
        ),
        sa.Column(
            "approved_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id"),
            nullable=True,
        ),
        sa.Column(
            "ledger_transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ledger_transactions.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("paid_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "provider", "provider_payout_id", name="uq_payouts_provider_ref"
        ),
    )
    op.create_index("ix_payouts_user_id", "payouts", ["user_id"])
    op.create_index("ix_payouts_season_id", "payouts", ["season_id"])
    op.create_index("ix_payouts_status", "payouts", ["status"])

    # ── Триггеры: раздельность касс, баланс, append-only ──────────────────
    op.execute(_ENFORCE_SEPARATION_FN)
    op.execute(
        "CREATE TRIGGER trg_ledger_separation "
        "BEFORE INSERT ON ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION enforce_ledger_separation();"
    )
    op.execute(_ENFORCE_BALANCED_FN)
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_ledger_balanced "
        "AFTER INSERT ON ledger_entries "
        "DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION enforce_transaction_balanced();"
    )
    op.execute(
        "CREATE TRIGGER trg_ledger_transactions_append_only "
        "BEFORE UPDATE OR DELETE ON ledger_transactions "
        "FOR EACH ROW EXECUTE FUNCTION block_mutations();"
    )
    op.execute(
        "CREATE TRIGGER trg_ledger_entries_append_only "
        "BEFORE UPDATE OR DELETE ON ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION block_mutations();"
    )

    # ── Засев плана счетов ────────────────────────────────────────────────
    # ВАЖНО: ledger_type объявляем enum-типом (не Text), иначе под asyncpg
    # значение биндится как VARCHAR без приведения и Postgres отклоняет вставку
    # ("column is of type ledger_type but expression is of type character varying").
    accounts_table = sa.table(
        "ledger_accounts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("ledger_type", ledger_type),
        sa.column("account_code", sa.Text()),
        sa.column("title", sa.Text()),
        sa.column("currency", sa.Text()),
    )
    op.bulk_insert(
        accounts_table,
        [
            {
                "id": uuid.uuid4(),
                "ledger_type": ltype,
                "account_code": code,
                "title": title,
                "currency": "RUB",
            }
            for ltype, code, title in _SEED_ACCOUNTS
        ],
    )


def downgrade() -> None:
    """Снимает триггеры/функции/таблицы/enum'ы billing (block_mutations — из 0008)."""
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ledger_entries_append_only ON ledger_entries;"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_ledger_transactions_append_only "
        "ON ledger_transactions;"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_balanced ON ledger_entries;")
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_separation ON ledger_entries;")
    op.execute("DROP FUNCTION IF EXISTS enforce_transaction_balanced();")
    op.execute("DROP FUNCTION IF EXISTS enforce_ledger_separation();")

    op.drop_table("payouts")
    op.drop_table("prize_funds")
    op.drop_table("payments")
    op.drop_table("subscriptions")
    op.drop_table("ledger_entries")
    op.drop_table("ledger_transactions")
    op.drop_table("ledger_accounts")

    bind = op.get_bind()
    for enum in reversed(_ALL_ENUMS):
        enum.drop(bind, checkfirst=True)
