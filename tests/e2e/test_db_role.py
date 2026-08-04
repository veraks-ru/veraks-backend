"""E2E: непривилегированная роль ``orakul_app`` физически не может UPDATE/DELETE
append-only-журналы (T9, второй контур защиты поверх триггеров block_mutations()).

Бутстрапит роль через боевой ``scripts/create_app_role.py`` (тем же кодом,
которым это делают в проде/локально), затем подключается ПОД ЭТОЙ РОЛЬЮ и
проверяет: SELECT/INSERT на append-only разрешены, UPDATE/DELETE — запрещены
на уровне привилегий (``permission denied``, до того как успеет сработать
триггер), а на обычной таблице роли доступен полный CRUD.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from urllib.parse import urlsplit, urlunsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio

_APP_ROLE = "orakul_app_e2e_test"
_APP_PASSWORD = "e2e-test-app-role-password"  # тестовый пароль, не боевой секрет
_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


def _app_role_url(owner_url: str) -> str:
    """Тот же хост/порт/БД, что у владельца, но под ролью приложения."""
    parts = urlsplit(owner_url)
    netloc = f"{_APP_ROLE}:{_APP_PASSWORD}@{parts.hostname}:{parts.port or 5432}"
    return urlunsplit(parts._replace(netloc=netloc))


@pytest.fixture(scope="module", autouse=True)
def _bootstrap_app_role(_migrated_database: None) -> None:
    """Создаёт (идемпотентно) роль ``orakul_app_e2e_test`` через боевой скрипт."""
    owner_url = os.environ["DATABASE_URL"]
    subprocess.run(
        [sys.executable, "scripts/create_app_role.py"],
        check=True,
        cwd=_BACKEND_ROOT,
        env={
            **os.environ,
            "DATABASE_URL": owner_url,
            "APP_DB_ROLE": _APP_ROLE,
            "APP_DB_PASSWORD": _APP_PASSWORD,
        },
    )


@pytest_asyncio.fixture
async def app_role_session(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """Сессия ПОД РОЛЬЮ ПРИЛОЖЕНИЯ к той же (уже усечённой fixture ``session``) БД."""
    owner_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(_app_role_url(owner_url))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        async with maker() as s:
            yield s
    finally:
        await engine.dispose()


async def _actor_type(session: AsyncSession) -> str:
    return (
        await session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'audit_actor_type' LIMIT 1"
            )
        )
    ).scalar_one()


async def test_app_role_can_select_and_insert_audit_log(
    session: AsyncSession, app_role_session: AsyncSession
) -> None:
    """Append-only: INSERT/SELECT разрешены роли приложения."""
    actor = await _actor_type(session)
    await app_role_session.execute(
        text(
            "INSERT INTO audit_log "
            "(occurred_at, actor_type, action, entity_type, hash) "
            "VALUES (now(), CAST(:actor AS audit_actor_type), 'e2e-role', 'e2e', 'h0')"
        ),
        {"actor": actor},
    )
    await app_role_session.commit()

    count = (
        await app_role_session.execute(text("SELECT count(*) FROM audit_log"))
    ).scalar_one()
    assert count == 1


async def test_app_role_cannot_update_audit_log(
    session: AsyncSession, app_role_session: AsyncSession
) -> None:
    """Append-only: UPDATE запрещён на уровне привилегий (REVOKE из 0011)."""
    actor = await _actor_type(session)
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(occurred_at, actor_type, action, entity_type, hash) "
            "VALUES (now(), CAST(:actor AS audit_actor_type), 'e2e-role', 'e2e', 'h0')"
        ),
        {"actor": actor},
    )
    await session.commit()

    with pytest.raises(DBAPIError) as exc:
        await app_role_session.execute(text("UPDATE audit_log SET action = 'hacked'"))
        await app_role_session.commit()
    assert "permission denied" in str(exc.value).lower()
    await app_role_session.rollback()


async def test_app_role_cannot_delete_from_ledger_entries(
    app_role_session: AsyncSession,
) -> None:
    """REVOKE применён и к ledger_entries (0011), не только к audit_log."""
    with pytest.raises(DBAPIError) as exc:
        await app_role_session.execute(text("DELETE FROM ledger_entries"))
        await app_role_session.commit()
    assert "permission denied" in str(exc.value).lower()
    await app_role_session.rollback()


async def test_app_role_cannot_update_user_consents(
    app_role_session: AsyncSession,
) -> None:
    """0025 user_consents — append-only, добавлена ПОСЛЕ 0011 (закрыто 0029)."""
    with pytest.raises(DBAPIError) as exc:
        await app_role_session.execute(text("UPDATE user_consents SET method = 'x'"))
        await app_role_session.commit()
    assert "permission denied" in str(exc.value).lower()
    await app_role_session.rollback()


async def test_app_role_has_full_crud_on_ordinary_table(
    session: AsyncSession, app_role_session: AsyncSession
) -> None:
    """Обычная (не append-only) таблица — полный CRUD роли приложения разрешён."""
    category_id = (
        await session.execute(
            text(
                "INSERT INTO categories (id, slug, title) "
                "VALUES (gen_random_uuid(), 'e2e-role-slug', 'e2e') "
                "RETURNING id"
            )
        )
    ).scalar_one()
    await session.commit()

    await app_role_session.execute(
        text("UPDATE categories SET title = 'e2e-updated' WHERE id = :id"),
        {"id": category_id},
    )
    await app_role_session.execute(
        text("DELETE FROM categories WHERE id = :id"), {"id": category_id}
    )
    await app_role_session.commit()

    count = (
        await session.execute(
            text("SELECT count(*) FROM categories WHERE id = :id"),
            {"id": category_id},
        )
    ).scalar_one()
    assert count == 0
