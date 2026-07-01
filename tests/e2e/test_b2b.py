"""E2E B2B signal API против реального Postgres: ключи, квота, сигналы, выручка."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.b2b.adapters.keygen import SecretsKeyGenerator
from app.modules.b2b.adapters.repository import SqlAlchemyApiKeyRepository
from app.modules.b2b.adapters.revenue import BillingRevenueRecorder
from app.modules.b2b.adapters.signal_gateway import SqlAlchemyB2bSignalGateway
from app.modules.b2b.application.use_cases import (
    AuthenticateApiKey,
    GetConsensusSignal,
    GetLeaderboardSignal,
    IssueApiKey,
    ListEventSignals,
    RevokeApiKey,
)
from app.modules.b2b.domain.errors import (
    InvalidApiKeyError,
    QuotaExceededError,
)
from app.modules.billing.adapters.repositories import SqlAlchemyLedgerRepository
from app.modules.billing.domain import chart
from tests.e2e.helpers import (
    add_active_season,
    add_category,
    add_open_event,
    add_user,
    place_locked_predictions,
)

pytestmark = pytest.mark.asyncio


class _FakeQuota:
    def __init__(self, cap: int = 10_000) -> None:
        self.cap = cap
        self._n: dict[uuid.UUID, int] = {}

    async def check_and_incr(self, key_id, *, daily_quota):  # noqa: ANN001
        self._n[key_id] = self._n.get(key_id, 0) + 1
        c = self._n[key_id]
        return c <= min(daily_quota, self.cap), c

    async def used_today(self, key_id):  # noqa: ANN001
        return self._n.get(key_id, 0)


def _issue_uc(session):  # noqa: ANN001
    return IssueApiKey(
        keys=SqlAlchemyApiKeyRepository(session),
        generator=SecretsKeyGenerator(),
        revenue=BillingRevenueRecorder(ledger=SqlAlchemyLedgerRepository(session)),
        default_quota=1000,
        price_kopecks=490_000,
    )


async def test_issue_authenticate_revoke_and_revenue(
    session: AsyncSession,
) -> None:
    owner = await add_user(session, username="b2b_owner")
    await session.flush()

    issued = await _issue_uc(session).execute(
        owner_user_id=owner.id, name="Аналитика Ромашки"
    )
    assert issued.plaintext.startswith("vk_")
    assert issued.key.key_prefix == issued.plaintext[:11]

    # Проводка выручки b2b ушла в операционную кассу (кредит revenue).
    ledger = SqlAlchemyLedgerRepository(session)
    revenue_acc = await ledger.get_account_by_code(chart.OPS_REVENUE_B2B)
    assert revenue_acc is not None
    assert await ledger.balance(revenue_acc.id) == -490_000

    keys = SqlAlchemyApiKeyRepository(session)
    quota = _FakeQuota()
    auth = AuthenticateApiKey(
        keys=keys, generator=SecretsKeyGenerator(), quota=quota
    )
    # Верный ключ проходит.
    authed = await auth.execute(plaintext=issued.plaintext)
    assert authed.id == issued.key.id
    # Неверный — 401-семантика.
    with pytest.raises(InvalidApiKeyError):
        await auth.execute(plaintext="vk_wrong")

    # Отзыв → ключ больше не аутентифицируется.
    await RevokeApiKey(keys=keys).execute(
        owner_user_id=owner.id, key_id=issued.key.id
    )
    with pytest.raises(InvalidApiKeyError):
        await auth.execute(plaintext=issued.plaintext)
    await session.commit()


async def test_quota_exceeded(session: AsyncSession) -> None:
    owner = await add_user(session, username="b2b_owner2")
    await session.flush()
    issued = await _issue_uc(session).execute(
        owner_user_id=owner.id, name="Лимит", daily_quota=2
    )
    keys = SqlAlchemyApiKeyRepository(session)
    auth = AuthenticateApiKey(
        keys=keys, generator=SecretsKeyGenerator(), quota=_FakeQuota()
    )
    await auth.execute(plaintext=issued.plaintext)  # 1
    await auth.execute(plaintext=issued.plaintext)  # 2
    with pytest.raises(QuotaExceededError):  # 3 — сверх квоты
        await auth.execute(plaintext=issued.plaintext)
    await session.commit()


async def test_signals_consensus_leaderboard_events(
    session: AsyncSession,
) -> None:
    admin = await add_user(session, username="b2b_admin")
    voters = [await add_user(session, username=f"b2b_v{i}") for i in range(5)]
    category = await add_category(session)
    season = await add_active_season(session)
    await session.flush()
    event = await add_open_event(
        session,
        category_id=category.id,
        created_by=admin.id,
        season_id=season.id,
    )
    await place_locked_predictions(
        session, event_id=event.id, user_ids=[v.id for v in voters]
    )
    # Глобальный рейтинг для сигнала лидерборда.
    await session.execute(
        text(
            "INSERT INTO ratings "
            "(id, user_id, scope_type, scope_id, mean_brier, skill_score, "
            " calibration_error, n_resolved, rank, updated_at) "
            "VALUES (gen_random_uuid(), :uid, CAST('global' AS rating_scope), "
            " NULL, 0.10000, 0.20000, 0.10000, 5, 1, now())"
        ),
        {"uid": str(voters[0].id)},
    )
    await session.flush()

    gateway = SqlAlchemyB2bSignalGateway(session)

    consensus = await GetConsensusSignal(gateway=gateway).execute(
        event_id=event.id
    )
    assert consensus.total_count == 5
    assert consensus.mean_probability is not None
    assert sum(consensus.distribution.values()) == 5

    board = await GetLeaderboardSignal(gateway=gateway).execute(
        scope="global", scope_id=None, limit=50
    )
    assert len(board) == 1
    assert board[0].username == "b2b_v0"

    events = await ListEventSignals(gateway=gateway).execute(
        status="open", limit=50
    )
    assert any(e.id == event.id for e in events)
    await session.commit()
