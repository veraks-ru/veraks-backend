"""T9: расширить REVOKE append-only-грантов на таблицы, добавленные после 0011

Миграция ``0011_revoke_append_only_grants`` фиксировала список append-only
таблиц (``audit_log``, ``resolutions``, ``ledger_transactions``,
``ledger_entries``) на момент своего написания. С тех пор триггером
``block_mutations()`` защитили ещё три журнала: ``season_finalizations`` и
``season_finalization_entries`` (0021), ``user_consents`` (0025) — но
привилегии роли приложения для них 0011 не трогал, так что до появления
роли/повторного запуска ``scripts/create_app_role.py`` они были прикрыты
только триггером, без второго рубежа на уровне GRANT/REVOKE.

Здесь — тот же паттерн, что в 0011: REVOKE UPDATE/DELETE у роли ``APP_DB_ROLE``
(по умолчанию ``orakul_app``), только если роль существует; иначе no-op —
триггеры остаются единственной гарантией. Полный актуальный список
append-only таблиц — в ``scripts/create_app_role.py``/``.sql`` (эти скрипты
переустанавливают гранты и для новых таблиц, добавленных позже: миграция
фиксирует только то, что известно на момент её написания, скрипт — то, что
реально есть в схеме на момент запуска).

Revision ID: 0029_extend_append_only_revoke
Revises: 0028_category_is_restricted
Create Date: 2026-08-04
"""
from __future__ import annotations

import os
from collections.abc import Sequence

from alembic import op

revision: str = "0029_extend_append_only_revoke"
down_revision: str | None = "0028_category_is_restricted"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Таблицы, ставшие append-only ПОСЛЕ 0011 (не входили в её список).
_NEW_APPEND_ONLY_TABLES = (
    "season_finalizations",
    "season_finalization_entries",
    "user_consents",
)


def _app_role() -> str:
    """Имя роли приложения (env ``APP_DB_ROLE``, дефолт ``orakul_app``)."""
    return os.environ.get("APP_DB_ROLE", "orakul_app")


def upgrade() -> None:
    """REVOKE UPDATE/DELETE на новых append-only таблицах у роли приложения."""
    role = _app_role()
    tables = ", ".join(_NEW_APPEND_ONLY_TABLES)
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
    tables = ", ".join(_NEW_APPEND_ONLY_TABLES)
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
