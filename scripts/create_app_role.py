"""Bootstrap непривилегированной роли БД приложения ``orakul_app`` (T9).

Второй контур защиты append-only-журналов поверх триггеров
``block_mutations()`` (миграции 0008 audit_log, 0009 resolutions, 0010
ledger_transactions/ledger_entries, 0021 season_finalizations/
season_finalization_entries, 0025 user_consents): у роли приложения физически
нет UPDATE/DELETE на эти таблицы — REVOKE из миграций 0011/0029 применяется
только при условии, что роль уже существует, иначе тихий no-op.

Идемпотентно: пароль и гранты переустанавливаются при каждом запуске —
это НУЖНО делать после каждой миграции, добавляющей таблицы (владелец схемы
создаёт их без прав приложения, пока не перевыполнен этот скрипт; см.
``ALTER DEFAULT PRIVILEGES`` внутри — покрывает будущие обычные таблицы
автоматически, но REVOKE на новых append-only всё равно нужно переиграть).

Запуск (после ``alembic upgrade head``, ПОД ВЛАДЕЛЬЦЕМ схемы — только у него
есть право GRANT/REVOKE/ALTER DEFAULT PRIVILEGES)::

    APP_DB_PASSWORD=... python scripts/create_app_role.py

Подключается через ``ALEMBIC_DATABASE_URL`` (владелец, если задан отдельно от
прикладного ``DATABASE_URL`` — см. .env.example), иначе — через
``DATABASE_URL``. Имя роли — ``APP_DB_ROLE`` (по умолчанию ``orakul_app``,
то же имя, что читает миграция 0011/0029).

Реализовано напрямую на ``asyncpg`` (уже зависимость приложения), а не через
внешний клиент ``psql`` — его может не быть в образе приложения. Полный
SQL-эквивалент для ручного запуска DBA — ``scripts/create_app_role.sql``
(держать логику синхронизированной при правках).
"""

from __future__ import annotations

import asyncio
import os

import asyncpg

# Список append-only таблиц — держать в синхронизации с триггерами
# block_mutations() (0008/0009/0010/0021/0025) и с REVOKE в миграциях
# 0011/0029. Проверка «сверить факт» — tests/e2e/test_db_role.py.
APPEND_ONLY_TABLES = (
    "audit_log",
    "resolutions",
    "ledger_transactions",
    "ledger_entries",
    "season_finalizations",
    "season_finalization_entries",
    "user_consents",
)


def _owner_url() -> str:
    """DSN владельца схемы (для asyncpg — без диалект-суффикса SQLAlchemy)."""
    url = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit(
            "Нужен DATABASE_URL или ALEMBIC_DATABASE_URL (владелец схемы, "
            "тот же, кем накатываются миграции)"
        )
    return url.replace("postgresql+asyncpg://", "postgresql://")


def _app_role() -> str:
    return os.environ.get("APP_DB_ROLE", "orakul_app")


def _app_password() -> str:
    password = os.environ.get("APP_DB_PASSWORD")
    if not password:
        raise SystemExit("Нужен APP_DB_PASSWORD — пароль роли приложения")
    return password


def _quote_ident(name: str) -> str:
    """Экранирование идентификатора (двойные кавычки) — как quote_ident()."""
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    """Экранирование строкового литерала (одинарные кавычки) — как quote_literal()."""
    return "'" + value.replace("'", "''") + "'"


async def bootstrap(conn: asyncpg.Connection, role: str, password: str) -> int:
    """Создаёт/обновляет роль и все гранты. Возвращает число защищённых
    append-only таблиц (реально присутствующих в схеме — для отчёта)."""
    role_q = _quote_ident(role)

    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    password_clause = f"LOGIN PASSWORD {_quote_literal(password)}"
    if exists:
        await conn.execute(f"ALTER ROLE {role_q} {password_clause}")
    else:
        await conn.execute(f"CREATE ROLE {role_q} {password_clause}")

    db_name = await conn.fetchval("SELECT current_database()")
    await conn.execute(f"GRANT CONNECT ON DATABASE {_quote_ident(db_name)} TO {role_q}")
    await conn.execute(f"GRANT USAGE ON SCHEMA public TO {role_q}")

    tables = [
        row["tablename"]
        for row in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
    ]
    append_only_present = 0
    for table in tables:
        table_q = _quote_ident(table)
        if table in APPEND_ONLY_TABLES:
            append_only_present += 1
            await conn.execute(f"GRANT SELECT, INSERT ON {table_q} TO {role_q}")
            await conn.execute(f"REVOKE UPDATE, DELETE ON {table_q} FROM {role_q}")
        else:
            await conn.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table_q} TO {role_q}"
            )

    sequences = [
        row["sequencename"]
        for row in await conn.fetch(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
        )
    ]
    for sequence in sequences:
        seq_q = _quote_ident(sequence)
        await conn.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq_q} TO {role_q}")

    owner = await conn.fetchval("SELECT current_user")
    owner_q = _quote_ident(owner)
    await conn.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_q} IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role_q}"
    )
    await conn.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_q} IN SCHEMA public "
        f"GRANT USAGE, SELECT ON SEQUENCES TO {role_q}"
    )
    return append_only_present


async def main() -> None:
    role = _app_role()
    password = _app_password()
    conn = await asyncpg.connect(_owner_url())
    try:
        protected = await bootstrap(conn, role, password)
    finally:
        await conn.close()
    print(
        f"Роль {role!r} создана/обновлена. Append-only таблиц под REVOKE "
        f"UPDATE/DELETE: {protected} из {len(APPEND_ONLY_TABLES)} ожидаемых "
        "(остальные появятся будущими миграциями — перезапустить скрипт после)."
    )


if __name__ == "__main__":
    asyncio.run(main())
