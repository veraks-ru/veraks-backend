"""Защита в глубину: REVOKE UPDATE/DELETE на append-only таблицах у роли приложения

Append-only уже гарантирован триггерами ``block_mutations()`` (миграции 0008–0010),
которые блокируют UPDATE/DELETE для кого угодно. Эта миграция добавляет второй
рубеж на уровне привилегий: у роли приложения физически нет права UPDATE/DELETE
на ``audit_log``, ``resolutions``, ``ledger_transactions``, ``ledger_entries``
(инвариант из CLAUDE.md §2.6). Так даже отключение/обход триггера не открывает
правку журналов — нужно эскалировать привилегии.

Имя роли приложения берётся из env ``APP_DB_ROLE`` (по умолчанию ``orakul_app``).
REVOKE применяется только если такая роль существует — иначе миграция —
no-op (CI/тестовые БД подключаются под владельцем и роли приложения не имеют).
Триггеры остаются единственной гарантией там, где отдельной роли нет.

Revision ID: 0011_revoke_append_only_grants
Revises: 0010_create_billing_ledger
Create Date: 2026-06-25
"""
from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0011_revoke_append_only_grants"
down_revision: str | None = "0010_create_billing_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_APPEND_ONLY_TABLES = (
    "audit_log",
    "resolutions",
    "ledger_transactions",
    "ledger_entries",
)


def _app_role() -> str:
    """Имя роли приложения (env ``APP_DB_ROLE``, дефолт ``orakul_app``)."""
    return os.environ.get("APP_DB_ROLE", "orakul_app")


def upgrade() -> None:
    """REVOKE UPDATE/DELETE на append-only таблицах у роли приложения (если есть)."""
    role = _app_role()
    tables = ", ".join(_APPEND_ONLY_TABLES)
    # Идемпотентно и безопасно: применяем только при наличии роли.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                REVOKE UPDATE, DELETE ON {tables} FROM {role};
            ELSE
                RAISE NOTICE 'Role % absent — append-only relies on triggers only', '{role}';
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    """Возвращает UPDATE/DELETE роли приложения (если есть). Триггеры всё равно блокируют."""
    role = _app_role()
    tables = ", ".join(_APPEND_ONLY_TABLES)
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}') THEN
                GRANT UPDATE, DELETE ON {tables} TO {role};
            END IF;
        END
        $$;
        """
    )
