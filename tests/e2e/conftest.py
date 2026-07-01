"""Харнесс e2e-тестов против РЕАЛЬНОГО Postgres.

В отличие от ``*/integration`` тестов (они поднимают FastAPI, но подменяют
I/O-порты фейками), здесь всё бьётся в настоящую БД: реальные миграции Alembic,
реальные репозитории/адаптеры, нативные PG-enum'ы, FK, UNIQUE и append-only
триггеры. Так проверяется именно тот шов, который фейки обойти не могут.

Требует ``DATABASE_URL``, указывающий на ВЫДЕЛЕННУЮ e2e-БД (в имени —
подстрока ``e2e``, чтобы случайно не снести dev/prod). База пересоздаётся и
мигрируется один раз за сессию; между тестами таблицы усекаются (``TRUNCATE
… CASCADE`` — append-only триггеры блокируют DELETE, но не TRUNCATE).

Запуск (внутри compose-сети, где хост БД — ``postgres``)::

    docker compose run --rm --no-deps \
      -e DATABASE_URL=postgresql+asyncpg://orakul:orakul@postgres:5432/orakul_e2e \
      -v $PWD/../backend:/app backend \
      sh -c "pip install -q pytest pytest-asyncio && python -m pytest tests/e2e -q"
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_DB_URL = os.environ.get("DATABASE_URL", "")


def _require_e2e_db() -> str:
    """URL выделенной e2e-БД или пропуск (защита от порчи dev/prod-базы)."""
    if not _DB_URL or "e2e" not in _DB_URL:
        pytest.skip(
            "e2e требует DATABASE_URL на выделенную БД с 'e2e' в имени "
            "(например postgresql+asyncpg://orakul:orakul@postgres:5432/orakul_e2e)"
        )
    return _DB_URL


async def _recreate_database(url: str) -> None:
    """DROP+CREATE тестовой БД через подключение к служебной ``postgres``."""
    import asyncpg  # локальный импорт: нужен только для админ-операций

    parts = urlsplit(url)
    dbname = parts.path.lstrip("/")
    conn = await asyncpg.connect(
        user=parts.username,
        password=parts.password,
        host=parts.hostname,
        port=parts.port or 5432,
        database="postgres",
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{dbname}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session", autouse=True)
def _migrated_database() -> Iterator[None]:
    """Пересоздаёт e2e-БД и накатывает миграции Alembic один раз за сессию."""
    url = _require_e2e_db()
    asyncio.run(_recreate_database(url))
    subprocess.run(
        ["alembic", "upgrade", "head"],
        check=True,
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
        env={**os.environ, "DATABASE_URL": url},
    )
    yield


async def _truncate_all(session: AsyncSession) -> None:
    """Усекает все таблицы — чистый лист между тестами.

    Исключения — статические справочные данные, засеянные миграциями (не
    тестовые): ``alembic_version``; ``ledger_accounts`` (план счетов, 0010);
    ``divisions`` (лестница дивизионов, 0016). Их усечение сломало бы
    billing/leagues-сценарии.
    """
    rows = (
        await session.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' "
                "AND tablename NOT IN "
                "('alembic_version', 'ledger_accounts', 'divisions')"
            )
        )
    ).scalars().all()
    if rows:
        joined = ", ".join(f'"{name}"' for name in rows)
        await session.execute(
            text(f"TRUNCATE {joined} RESTART IDENTITY CASCADE")
        )
        await session.commit()


@pytest_asyncio.fixture
async def session(_migrated_database: None) -> AsyncIterator[AsyncSession]:
    """Чистая сессia к реальному Postgres (таблицы усечены перед тестом)."""
    engine = create_async_engine(_DB_URL)
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with maker() as prep:
        await _truncate_all(prep)
    try:
        async with maker() as s:
            yield s
    finally:
        await engine.dispose()
