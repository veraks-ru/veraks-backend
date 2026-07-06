"""Защита денег: уникальность проводки выплаты и неизменяемость кассы счёта.

Два инварианта на уровне БД (страховка поверх прикладной логики):

* Частичный UNIQUE на ``ledger_transactions(external_ref) WHERE kind='prize_payout'``
  — вторая проводка выплаты с тем же ``external_ref`` (= id выплаты) невозможна,
  даже если прикладная блокировка ``FOR UPDATE`` будет обойдена. Защита от
  двойного списания приза (C1).

* Триггер ``enforce_ledger_account_immutable`` (BEFORE UPDATE ON ledger_accounts)
  — запрещает менять ``ledger_type`` и ``account_code`` уже существующего счёта.
  Без него ``UPDATE ledger_accounts SET ledger_type=...`` ретроактивно «переносил»
  бы все ноги счёта в другую кассу в обход разделения касс (M-LEDGERACC).
"""

from __future__ import annotations

from alembic import op

revision: str = "0019_ledger_payout_guards"
down_revision: str | None = "0018_create_api_keys"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        "CREATE UNIQUE INDEX uq_ledger_txn_prize_payout_ref "
        "ON ledger_transactions (external_ref) "
        "WHERE kind = 'prize_payout' AND external_ref IS NOT NULL"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION enforce_ledger_account_immutable()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.ledger_type <> OLD.ledger_type THEN
                RAISE EXCEPTION
                    'ledger_accounts.ledger_type неизменяем (счёт %): % -> %',
                    OLD.id, OLD.ledger_type, NEW.ledger_type;
            END IF;
            IF NEW.account_code <> OLD.account_code THEN
                RAISE EXCEPTION
                    'ledger_accounts.account_code неизменяем (счёт %)', OLD.id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_ledger_accounts_immutable "
        "BEFORE UPDATE ON ledger_accounts "
        "FOR EACH ROW EXECUTE FUNCTION enforce_ledger_account_immutable();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_ledger_accounts_immutable ON ledger_accounts")
    op.execute("DROP FUNCTION IF EXISTS enforce_ledger_account_immutable()")
    op.execute("DROP INDEX IF EXISTS uq_ledger_txn_prize_payout_ref")
