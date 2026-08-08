"""Полный сброс рантайм-данных: события, прогнозы, сезоны и всё связанное.

Нужен при переходе из демо-режима в боевой: фейковые события и участники
отпугивают живых пользователей. Сносит ВЕСЬ рантайм, оставляя только
справочники из миграций — дивизионы (0016) и базовый план счетов (0010).

По умолчанию — **сухой прогон**: печатает, сколько строк будет удалено, и
ничего не трогает. Настоящее удаление требует трёх независимых подтверждений:

    python scripts/reset_runtime_data.py                      # только счётчики
    python scripts/reset_runtime_data.py --expect-db orakul   # счётчики + сверка имени БД
    python scripts/reset_runtime_data.py --expect-db orakul --apply --yes-i-understand

``--expect-db`` обязателен для ``--apply`` и сверяется с именем целевой базы:
это защита от запуска по ошибочно экспортированному окружению.

Адрес БД берётся из ``DATABASE_URL``; при работе через ``kubectl port-forward``
его задаёт ``--database-url`` (окружение в этот момент смотрит на локальный
дев, а нужен прод на ``localhost:<проброшенный порт>``).

ВНИМАНИЕ, ЮРИДИЧЕСКОЕ. ``audit_log``, ``resolutions``, ``ledger_*``,
``season_finalizations`` и ``user_consents`` — append-only по конструкции и по
требованиям PRD §7 (публичный конкурс, гл. 57 ГК; 54-ФЗ по платежам).
``TRUNCATE`` обходит блокирующий DELETE-триггер, поэтому скрипт физически
способен стереть то, что удалять нельзя, если в базе есть настоящие деньги,
акцепты оферты или объявленные результаты сезона. Счётчики ниже показывают это
явно — сверьтесь с ними до ``--apply``.

Права: TRUNCATE требует владельца таблиц. У прикладной роли (``orakul_app``)
их нет — запускать под миграционной ролью (``ALEMBIC_DATABASE_URL``-креды).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings
from app.db.session import session_scope

# Порядок не важен — TRUNCATE ... CASCADE снимает FK разом, одной командой.
# Список синхронизирован с ``reset()`` в seed.py.
RUNTIME_TABLES = [
    "users",
    "categories",
    "seasons",
    "events",
    "predictions",
    "resolutions",
    "disputes",
    "ratings",
    "ledger_entries",
    "ledger_transactions",
    "audit_log",
    "season_finalizations",
    "season_finalization_entries",
    "resolution_scoring_dispatches",
    "notifications",
    "comments",
    "follows",
    "subscriptions",
    "payments",
    "prize_funds",
    "payouts",
    "api_keys",
    "leagues",
    "league_memberships",
    "division_memberships",
]

# Сносятся каскадом от ``users`` (FK), но считаем и показываем их отдельно —
# иначе «полный сброс» молча уносил бы акцепты оферты и платёжные реквизиты.
CASCADED_TABLES = ["user_consents", "payout_requisites"]

# Строки, требующие отдельного внимания перед сбросом: деньги, юридические
# следы и объявленные результаты конкурса.
SENSITIVE_TABLES = {
    "audit_log",
    "ledger_entries",
    "ledger_transactions",
    "payments",
    "payouts",
    "prize_funds",
    "resolutions",
    "season_finalizations",
    "user_consents",
}


@asynccontextmanager
async def _session(database_url: str | None) -> AsyncIterator[AsyncSession]:
    """Сессия к целевой БД: своя при ``--database-url``, иначе общая из настроек.

    Отдельный движок нужен для работы через port-forward: окружение в этот
    момент указывает на локальный дев, а трогать надо прод на ``localhost:5433``.
    """
    if database_url is None:
        async with session_scope() as session:
            yield session
        return

    engine = create_async_engine(database_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    finally:
        await engine.dispose()


def _db_name(url: str) -> str:
    """Имя базы из DATABASE_URL (без драйвера и параметров)."""
    return urlparse(url.replace("+asyncpg", "")).path.lstrip("/")


def _db_host(url: str) -> str:
    parsed = urlparse(url.replace("+asyncpg", ""))
    return f"{parsed.hostname}:{parsed.port or 5432}"


async def collect_counts(session: AsyncSession) -> dict[str, int]:
    """Считает строки во всех затрагиваемых таблицах.

    Имена таблиц — из констант модуля, не из пользовательского ввода, поэтому
    интерполяция в SQL здесь безопасна (параметризовать имя таблицы нельзя).
    """
    counts: dict[str, int] = {}
    for table in [*RUNTIME_TABLES, *CASCADED_TABLES]:
        result = await session.execute(text(f"SELECT count(*) FROM {table}"))
        counts[table] = int(result.scalar_one())
    return counts


def print_report(counts: dict[str, int], *, db: str, host: str) -> int:
    """Печатает отчёт; возвращает общее число строк под удаление."""
    width = max(len(t) for t in counts)
    print(f"\nБаза: {db}  ({host})")
    print("─" * (width + 24))
    total = 0
    for table, n in counts.items():
        total += n
        mark = ""
        if table in SENSITIVE_TABLES and n:
            mark = "  ← append-only / деньги"
        cascade = "  (каскадом)" if table in CASCADED_TABLES else ""
        print(f"{table.ljust(width)}  {str(n).rjust(8)}{cascade}{mark}")
    print("─" * (width + 24))
    print(f"{'ИТОГО строк'.ljust(width)}  {str(total).rjust(8)}")

    risky = {t: counts[t] for t in SENSITIVE_TABLES if counts.get(t)}
    if risky:
        print(
            "\nВНИМАНИЕ: непустые append-only/денежные таблицы — "
            + ", ".join(f"{t}={n}" for t, n in sorted(risky.items()))
        )
        print(
            "Если это не демо-данные, удаление уничтожит юридически значимые "
            "записи (PRD §7). Сверьтесь до --apply."
        )
    print(
        "\nСохраняются: divisions (миграция 0016) и базовый план счетов "
        "ledger_accounts ops:/prize: (миграция 0010)."
    )
    return total


# Разбор «опасных» строк: перед необратимым шагом важно видеть не только
# сколько строк удаляем, но и что за ними стоит — настоящие ли это деньги.
# Только чтение и только неперсональные поля: реквизиты выплат зашифрованы и
# здесь не расшифровываются.
DETAIL_QUERIES: list[tuple[str, str]] = [
    (
        "Пользователи",
        (
            "SELECT username, role, status, "
            "to_char(created_at,'YYYY-MM-DD HH24:MI') AS created "
            "FROM users ORDER BY created_at"
        ),
    ),
    (
        "Платежи",
        (
            "SELECT provider, purpose, status, amount_kopecks, "
            "to_char(created_at,'YYYY-MM-DD') AS created, "
            "(fiscal_receipt_id IS NOT NULL) AS has_receipt "
            "FROM payments ORDER BY created_at"
        ),
    ),
    (
        "Выплаты",
        (
            "SELECT status, amount_kopecks, tax_withheld_kopecks, provider, "
            "(provider_payout_id IS NOT NULL) AS sent_to_provider, "
            "to_char(created_at,'YYYY-MM-DD') AS created "
            "FROM payouts ORDER BY created_at"
        ),
    ),
    (
        "Призовые фонды",
        (
            "SELECT sponsor_name, status, committed_kopecks, deposited_kopecks, "
            "to_char(created_at,'YYYY-MM-DD') AS created "
            "FROM prize_funds ORDER BY created_at"
        ),
    ),
    (
        "Проводки леджера",
        (
            "SELECT ledger_type, kind, count(*) AS n, "
            "sum(e.amount_kopecks) AS sum_kop "
            "FROM ledger_transactions t "
            "JOIN ledger_entries e ON e.transaction_id = t.id "
            "GROUP BY ledger_type, kind ORDER BY ledger_type, kind"
        ),
    ),
    (
        "Акцепты оферты",
        (
            "SELECT u.username, c.document, c.version, c.method, "
            "to_char(c.accepted_at,'YYYY-MM-DD HH24:MI') AS accepted "
            "FROM user_consents c JOIN users u ON u.id = c.user_id "
            "ORDER BY c.accepted_at"
        ),
    ),
]


async def print_detail(session: AsyncSession) -> None:
    """Печатает содержимое денежных и юридически значимых таблиц."""
    for title, sql in DETAIL_QUERIES:
        result = await session.execute(text(sql))
        rows = result.mappings().all()
        print(f"\n── {title} ({len(rows)}) " + "─" * max(0, 44 - len(title)))
        if not rows:
            print("  пусто")
            continue
        cols = list(rows[0].keys())
        widths = {
            c: max(len(c), *(len(str(r[c])) for r in rows)) for c in cols
        }
        print("  " + "  ".join(c.ljust(widths[c]) for c in cols))
        for row in rows:
            print("  " + "  ".join(str(row[c]).ljust(widths[c]) for c in cols))


async def apply_reset(session: AsyncSession) -> None:
    """Один TRUNCATE на всё + удаление рантайм-счетов призовых фондов."""
    await session.execute(
        text(f"TRUNCATE TABLE {', '.join(RUNTIME_TABLES)} RESTART IDENTITY CASCADE")
    )
    # Счета фондов создаются при анонсе; базовый план счетов из 0010 сохраняем.
    await session.execute(
        text("DELETE FROM ledger_accounts WHERE account_code LIKE 'prize:fund:%'")
    )


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Полный сброс рантайм-данных (по умолчанию — сухой прогон)."
    )
    parser.add_argument(
        "--database-url",
        help=(
            "Адрес БД; по умолчанию берётся из DATABASE_URL. Нужен при работе "
            "через port-forward, когда окружение указывает на локальный дев."
        ),
    )
    parser.add_argument(
        "--database-url-file",
        help=(
            "Файл с адресом БД одной строкой. Предпочтительнее "
            "--database-url: пароль не попадает ни в историю команд, ни в "
            "список процессов."
        ),
    )
    parser.add_argument(
        "--expect-db",
        help="Ожидаемое имя базы; сверяется с адресом БД. Обязателен для --apply.",
    )
    parser.add_argument(
        "--detail",
        action="store_true",
        help=(
            "Показать содержимое денежных и юридически значимых таблиц "
            "(только чтение, без расшифровки персональных данных)."
        ),
    )
    parser.add_argument(
        "--apply", action="store_true", help="Выполнить удаление (иначе сухой прогон)."
    )
    parser.add_argument(
        "--yes-i-understand",
        action="store_true",
        help="Подтверждение необратимости; обязателен вместе с --apply.",
    )
    args = parser.parse_args()

    if args.database_url and args.database_url_file:
        print(
            "ОТКАЗ: --database-url и --database-url-file взаимоисключающие.",
            file=sys.stderr,
        )
        return 2
    if args.database_url_file:
        try:
            args.database_url = Path(args.database_url_file).read_text().strip()
        except OSError as exc:
            print(f"ОТКАЗ: не читается {args.database_url_file}: {exc}", file=sys.stderr)
            return 2
        if not args.database_url:
            print(f"ОТКАЗ: {args.database_url_file} пуст.", file=sys.stderr)
            return 2

    url = args.database_url or get_settings().database_url
    db, host = _db_name(url), _db_host(url)

    if args.expect_db and args.expect_db != db:
        print(
            f"ОТКАЗ: ожидалась база «{args.expect_db}», а адрес БД "
            f"указывает на «{db}» ({host}).",
            file=sys.stderr,
        )
        return 2

    async with _session(args.database_url) as session:
        counts = await collect_counts(session)
        total = print_report(counts, db=db, host=host)

        if args.detail:
            await print_detail(session)

        if not args.apply:
            via = (
                f" --database-url-file {args.database_url_file}"
                if args.database_url_file
                else " --database-url <тот же адрес>"
                if args.database_url
                else ""
            )
            print("\nСухой прогон — ничего не удалено. Для удаления:")
            print(
                f"  python scripts/reset_runtime_data.py{via} "
                f"--expect-db {db} --apply --yes-i-understand"
            )
            return 0

        if not args.expect_db:
            print("\nОТКАЗ: --apply требует --expect-db.", file=sys.stderr)
            return 2
        if not args.yes_i_understand:
            print(
                "\nОТКАЗ: --apply требует --yes-i-understand.", file=sys.stderr
            )
            return 2

        print(f"\nУдаляю {total} строк из «{db}» ({host})…")
        await apply_reset(session)

    print("Готово. База пуста: заводите категории, сезон и события заново.")
    print("Дальше: /admin/events → категории и события, /admin/seasons → сезон.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
