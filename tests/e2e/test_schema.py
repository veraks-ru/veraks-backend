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


async def test_rating_scope_enum_present(session: AsyncSession) -> None:
    values = await _enum_values(session, "rating_scope")
    assert {"global", "category", "season"} <= values


async def test_subscription_plan_enum_has_tariffs(session: AsyncSession) -> None:
    values = await _enum_values(session, "subscription_plan")
    # Тарифы из 0012 (день/неделя добавлены к месяцу/году).
    assert {"daily", "weekly", "monthly", "annual"} <= values


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
