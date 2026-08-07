"""Эксплуатационные операции над аккаунтом: роль, подтверждение личности,
перенос реквизитов выплат.

Зачем скрипт, а не админка. Первую админскую роль выдать через API нельзя:
``POST /admin/*`` сам требует администратора, а после включения входа по
email демо-аккаунты остались без адреса и войти под ними невозможно —
классическая задача bootstrap. Раньше это решалось разовым ``psql -c
"UPDATE users…"`` по живой базе: не воспроизводимо, без проверок и без
следа. Здесь та же операция оформлена как проверяемая процедура —
в одной транзакции, с выводом состояния до и после, идемпотентно.

Смена роли и отметка ``identity_verified`` намеренно НЕ пишутся в
``audit_log``: журнал фиксирует действия внутри продукта (кто из
администраторов что сделал), а это внешняя эксплуатационная операция
владельца платформы — её след остаётся в истории запусков скрипта и в
выводе ниже, а не в продуктовом аудите, где нет актора-администратора.

Перенос реквизитов выплат (``--move-payout-requisites-from``) меняет
владельца строки в ``payout_requisites``. Она хранит зашифрованные телефон
СБП и ФИО получателя: расшифровка тут не нужна и не делается — меняется
только привязка ``user_id``.

Что скрипт НЕ умеет намеренно: переносить прогнозы, рейтинги и подписки.
Трек-рекорд заработан конкретным аккаунтом, и его передача сфальсифицировала
бы публичную статистику точности и лидерборды; подписки — финансовая история,
привязанная к платежам конкретного плательщика.

Запуск (нужен доступ к БД; в кластере — из пода бэкенда)::

    python scripts/manage_user.py --username andrey --role admin --verified
    python scripts/manage_user.py --username andrey \\
        --move-payout-requisites-from kalibr
    python scripts/manage_user.py --username kalibr --role user

Подключение — по ``DATABASE_URL`` (или ``ALEMBIC_DATABASE_URL``, если
прикладная роль ограничена в правах). Реализовано на ``asyncpg``, как и
``create_app_role.py``: внешнего ``psql`` в образе приложения нет.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

import asyncpg

_ROLES = ("user", "editor", "arbiter", "admin")


def _dsn() -> str:
    """DSN для asyncpg: SQLAlchemy-схему ``postgresql+asyncpg://`` не понимает."""
    raw = os.environ.get("ALEMBIC_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not raw:
        raise SystemExit("Не задан DATABASE_URL (или ALEMBIC_DATABASE_URL).")
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _fetch_user(conn: asyncpg.Connection, username: str) -> Any:
    return await conn.fetchrow(
        "SELECT id, username, display_name, role, status, identity_verified, email"
        " FROM users WHERE username = $1",
        username,
    )


def _describe(row: Any) -> str:
    return (
        f"@{row['username']} (id={row['id']}) роль={row['role']} "
        f"статус={row['status']} подтверждён={row['identity_verified']} "
        f"email={row['email'] or '—'}"
    )


async def _run(args: argparse.Namespace) -> int:
    conn = await asyncpg.connect(_dsn())
    try:
        async with conn.transaction():
            target = await _fetch_user(conn, args.username)
            if target is None:
                print(f"Пользователь @{args.username} не найден.", file=sys.stderr)
                return 1
            print("До:  " + _describe(target))

            if args.role is not None:
                await conn.execute(
                    "UPDATE users SET role = $1::user_role WHERE id = $2",
                    args.role,
                    target["id"],
                )
            if args.verified is not None:
                await conn.execute(
                    "UPDATE users SET identity_verified = $1 WHERE id = $2",
                    args.verified,
                    target["id"],
                )

            if args.move_payout_requisites_from:
                donor = await _fetch_user(conn, args.move_payout_requisites_from)
                if donor is None:
                    print(
                        f"Аккаунт-источник @{args.move_payout_requisites_from} не найден.",
                        file=sys.stderr,
                    )
                    return 1
                # У получателя уже могут быть свои реквизиты: две строки на
                # одного пользователя сделали бы выбор счёта неоднозначным,
                # поэтому переносим только на пустое место.
                existing = await conn.fetchval(
                    "SELECT count(*) FROM payout_requisites WHERE user_id = $1",
                    target["id"],
                )
                if existing:
                    print(
                        f"У @{args.username} уже есть реквизиты выплат — перенос пропущен.",
                        file=sys.stderr,
                    )
                    return 1
                moved = await conn.execute(
                    "UPDATE payout_requisites SET user_id = $1, updated_at = now()"
                    " WHERE user_id = $2",
                    target["id"],
                    donor["id"],
                )
                print(f"Реквизиты выплат: {moved} (от @{donor['username']})")

            after = await _fetch_user(conn, args.username)
            print("После: " + _describe(after))
        return 0
    finally:
        await conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True, help="кого меняем")
    parser.add_argument("--role", choices=_ROLES, help="новая роль")
    verified = parser.add_mutually_exclusive_group()
    verified.add_argument(
        "--verified",
        dest="verified",
        action="store_true",
        default=None,
        help="отметить личность подтверждённой",
    )
    verified.add_argument(
        "--unverified",
        dest="verified",
        action="store_false",
        help="снять отметку подтверждения",
    )
    parser.add_argument(
        "--move-payout-requisites-from",
        metavar="USERNAME",
        help="перенести реквизиты выплат с этого аккаунта",
    )
    args = parser.parse_args()
    if args.role is None and args.verified is None and not args.move_payout_requisites_from:
        parser.error("нечего делать: укажите --role, --verified/--unverified или перенос")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
