"""E2E аудит-цепочки против реального Postgres (ревью T10, фикс-раунд 1).

Critical-1: ``ImmediatelyCommittingAuditTrail`` должна коммитить запись в
СВОЕЙ транзакции — независимо от того, что происходит с транзакцией
«внешнего» запроса дальше (в частности, от её отката). Фейковые
integration-тесты (``FakeAuditTrail`` в памяти) этого доказать не могут: там
нет настоящих транзакций/откатов — нужен реальный Postgres.

Important-1: тавтологичности тестов верификации (там цепочка строится тем же
``entry_payload`` в памяти, что и проверяется) здесь нет — пишем реальным
``SqlAlchemyAuditTrail`` в реальную БД (datetime/UUID/кириллица в payload
проходят через настоящий ``jsonb``-круглый путь) и проверяем ``VerifyAuditChain``
над тем же хранилищем.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.shared.audit.adapters.orm import AuditLogORM
from app.shared.audit.adapters.reader import SqlAlchemyAuditLogReader
from app.shared.audit.adapters.trail import (
    ImmediatelyCommittingAuditTrail,
    SqlAlchemyAuditTrail,
)
from app.shared.audit.application.verify_chain import VerifyAuditChain
from app.shared.audit.domain.entities import AuditActorType


async def test_immediate_commit_survives_outer_rollback_unlike_shared_session(
    session: AsyncSession,
) -> None:
    """Контраст «до фикса / после фикса» на одной и той же реальной транзакции.

    ``session`` здесь играет роль сессии HTTP-запроса (как в проде через
    ``get_session``). Пишем ОБОИМИ способами в неё же/через неё, потом
    откатываем — как ``get_session`` делает при любом исключении, — и
    смотрим, что осталось.
    """
    shared_entity_id = uuid.uuid4()
    immediate_entity_id = uuid.uuid4()

    # Старый (баговый) способ — через ТУ ЖЕ сессию, что и «запрос».
    await SqlAlchemyAuditTrail(session).record(
        actor_id=None,
        actor_type=AuditActorType.SYSTEM,
        action="identity.refresh.reuse_detected",
        entity_type="user",
        entity_id=shared_entity_id,
    )

    # Фикс: своя короткая транзакция, коммитится сразу внутри record().
    recorded = await ImmediatelyCommittingAuditTrail().record(
        actor_id=None,
        actor_type=AuditActorType.SYSTEM,
        action="identity.refresh.reuse_detected",
        entity_type="user",
        entity_id=immediate_entity_id,
    )
    assert recorded.id is not None

    # «Запрос» падает: get_session делает rollback транзакции запроса.
    await session.rollback()

    shared_row = (
        await session.execute(
            select(AuditLogORM).where(AuditLogORM.entity_id == shared_entity_id)
        )
    ).scalar_one_or_none()
    immediate_row = (
        await session.execute(
            select(AuditLogORM).where(AuditLogORM.entity_id == immediate_entity_id)
        )
    ).scalar_one_or_none()

    # Старый путь: запись жила в транзакции запроса → откатилась вместе с ней.
    assert shared_row is None
    # Фикс: запись уже закоммичена в своей транзакции → пережила откат.
    assert immediate_row is not None
    assert immediate_row.action == "identity.refresh.reuse_detected"


async def test_verify_chain_ok_against_real_writes_with_mixed_types(
    session: AsyncSession,
) -> None:
    """Пишем реальным ``SqlAlchemyAuditTrail`` разнотипные записи и верифицируем
    их же реальным ``VerifyAuditChain`` над той же БД — цепочка цела.

    Проверяет, что круглый путь через ``jsonb`` (UUID/datetime уже приведены
    к строкам в ``entry_payload`` до вставки — см. её докстринг про
    ограничение типов) и кириллица в текстовых полях не дают ложных
    расхождений хеша.
    """
    trail = SqlAlchemyAuditTrail(session)
    user_id = uuid.uuid4()
    event_id = uuid.uuid4()
    season_id = uuid.uuid4()

    await trail.record(
        actor_id=user_id,
        actor_type=AuditActorType.USER,
        action="identity.login",
        entity_type="user",
        entity_id=user_id,
        metadata={"is_new_user": True},
    )
    await trail.record(
        actor_id=user_id,
        actor_type=AuditActorType.ARBITER,
        action="event.annulled",
        entity_type="event",
        entity_id=event_id,
        before={"status": "resolved"},
        after={
            "status": "annulled",
            "reason": "Спор неразрешим по заданному источнику — ст. 1058 ГК РФ",
        },
        metadata={"note": "Аннулировано арбитром после разбора обращения"},
    )
    await trail.record(
        actor_id=None,
        actor_type=AuditActorType.SYSTEM,
        action="season.finalized",
        entity_type="season",
        entity_id=season_id,
        after={"status": "finished", "qualified_count": 3, "total_participants": 5},
    )
    await session.commit()

    result = await VerifyAuditChain(reader=SqlAlchemyAuditLogReader(session)).execute()

    assert result.ok is True
    assert result.checked == 3
    assert result.first_broken_id is None
