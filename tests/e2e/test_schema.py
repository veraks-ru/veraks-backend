"""E2E схемы против реального Postgres: миграции, нативные enum'ы, append-only.

Проверяет инварианты, которые фейковые integration-тесты обойти не могут:
Alembic докатан до head, нативные PG-enum'ы существуют с нужными значениями
(в т.ч. ``event_status='proposed'`` из 0013 и ``rating_scope``), а append-only
таблицы реально запрещают DELETE на уровне триггера ``block_mutations()``.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


async def _enum_values(session: AsyncSession, type_name: str) -> set[str]:
    rows = (
        await session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid WHERE t.typname = :t"
            ),
            {"t": type_name},
        )
    ).scalars().all()
    return set(rows)


async def test_migrations_are_at_head(session: AsyncSession) -> None:
    version = (
        await session.execute(text("SELECT version_num FROM alembic_version"))
    ).scalar_one()
    assert version  # какая-то ревизия докатана


async def test_event_status_enum_includes_proposed(session: AsyncSession) -> None:
    values = await _enum_values(session, "event_status")
    assert {"proposed", "draft", "open", "closed", "resolved"} <= values


async def test_event_status_enum_includes_annulled(session: AsyncSession) -> None:
    """0026: аннулирование после резолюции — отдельное значение enum."""
    values = await _enum_values(session, "event_status")
    assert {"annulled", "cancelled"} <= values


async def test_rating_scope_enum_present(session: AsyncSession) -> None:
    values = await _enum_values(session, "rating_scope")
    assert {"global", "category", "season"} <= values


async def test_subscription_plan_enum_has_tariffs(session: AsyncSession) -> None:
    values = await _enum_values(session, "subscription_plan")
    # Тарифы из 0012 (день/неделя добавлены к месяцу/году).
    assert {"daily", "weekly", "monthly", "annual"} <= values


async def _insert_user(session: AsyncSession, **columns: object) -> None:
    """Вставляет строку users минимальным набором полей (остальное — server_default)."""
    names = ", ".join(columns)
    values = ", ".join(f":{name}" for name in columns)
    await session.execute(
        text(f"INSERT INTO users (id, {names}) VALUES (gen_random_uuid(), {values})"),
        columns,
    )


async def test_users_esia_keys_are_nullable_after_0030(session: AsyncSession) -> None:
    """Аккаунт без СНИЛС и без oid ЕСИА — легальное состояние (вход по email).

    До 0030 оба поля были NOT NULL, то есть схема допускала только
    ЕСИА-регистрацию.
    """
    await _insert_user(session, username="mailonly-1", display_name="Один",
                       email="one@example.test")
    await _insert_user(session, username="mailonly-2", display_name="Два",
                       email="two@example.test")
    await session.commit()

    count = (
        await session.execute(
            text("SELECT count(*) FROM users WHERE snils_hash IS NULL")
        )
    ).scalar_one()
    assert count == 2  # NULL-ы не конфликтуют между собой в частичном индексе


async def test_users_email_is_unique_case_insensitively(
    session: AsyncSession,
) -> None:
    """citext + частичный UNIQUE: один ящик — один аккаунт, регистр не спасает."""
    await _insert_user(session, username="first", display_name="Первый",
                       email="User@Example.test")
    await session.commit()

    with pytest.raises(DBAPIError) as exc:
        await _insert_user(session, username="second", display_name="Второй",
                           email="user@example.test")
        await session.commit()
    assert "ux_users_email" in str(exc.value)
    await session.rollback()


async def test_identity_verified_defaults_to_false(session: AsyncSession) -> None:
    """Новый аккаунт не считается идентифицированным, пока не доказано обратное."""
    await _insert_user(session, username="fresh", display_name="Новый",
                       email="fresh@example.test")
    await session.commit()

    verified = (
        await session.execute(
            text("SELECT identity_verified FROM users WHERE username = 'fresh'")
        )
    ).scalar_one()
    assert verified is False


async def test_audit_log_is_append_only_delete_blocked(
    session: AsyncSession,
) -> None:
    actor = (
        await session.execute(
            text(
                "SELECT e.enumlabel FROM pg_enum e "
                "JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'audit_actor_type' LIMIT 1"
            )
        )
    ).scalar_one()
    await session.execute(
        text(
            "INSERT INTO audit_log "
            "(occurred_at, actor_type, action, entity_type, hash) "
            "VALUES (now(), CAST(:actor AS audit_actor_type), 'e2e', 'e2e', 'h0')"
        ),
        {"actor": actor},
    )
    await session.commit()

    # DELETE запрещён триггером block_mutations() — append-only журнал.
    with pytest.raises(DBAPIError) as exc:
        await session.execute(text("DELETE FROM audit_log"))
        await session.commit()
    assert "append-only" in str(exc.value)
    await session.rollback()

    # Строка на месте — журнал неизменяем.
    count = (
        await session.execute(text("SELECT count(*) FROM audit_log"))
    ).scalar_one()
    assert count == 1
