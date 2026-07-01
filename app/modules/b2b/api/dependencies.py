"""Composition root модуля b2b: use-cases + аутентификация по X-API-Key."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Header
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.session import get_session
from app.modules.b2b.adapters.keygen import SecretsKeyGenerator
from app.modules.b2b.adapters.quota import RedisQuotaCounter
from app.modules.b2b.adapters.repository import SqlAlchemyApiKeyRepository
from app.modules.b2b.adapters.revenue import BillingRevenueRecorder
from app.modules.b2b.adapters.signal_gateway import SqlAlchemyB2bSignalGateway
from app.modules.b2b.application.use_cases import (
    AuthenticateApiKey,
    GetConsensusSignal,
    GetKeyUsage,
    GetLeaderboardSignal,
    IssueApiKey,
    ListEventSignals,
    ListMyApiKeys,
    RevokeApiKey,
)
from app.modules.b2b.domain.entities import ApiKey
from app.modules.b2b.domain.errors import InvalidApiKeyError
from app.modules.billing.adapters.repositories import SqlAlchemyLedgerRepository
from app.modules.identity.api.dependencies import get_redis_client

SessionDep = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]


def get_quota_counter(redis: RedisDep) -> RedisQuotaCounter:
    return RedisQuotaCounter(redis)


QuotaDep = Annotated[RedisQuotaCounter, Depends(get_quota_counter)]


# ── Управление ключами (JWT-владелец) ────────────────────────────────────────


def get_issue_api_key(session: SessionDep) -> IssueApiKey:
    b2b = get_settings().b2b
    return IssueApiKey(
        keys=SqlAlchemyApiKeyRepository(session),
        generator=SecretsKeyGenerator(),
        revenue=BillingRevenueRecorder(
            ledger=SqlAlchemyLedgerRepository(session)
        ),
        default_quota=b2b.default_daily_quota,
        price_kopecks=b2b.key_price_kopecks,
    )


def get_list_my_api_keys(session: SessionDep) -> ListMyApiKeys:
    return ListMyApiKeys(keys=SqlAlchemyApiKeyRepository(session))


def get_revoke_api_key(session: SessionDep) -> RevokeApiKey:
    return RevokeApiKey(keys=SqlAlchemyApiKeyRepository(session))


def get_key_usage(session: SessionDep, quota: QuotaDep) -> GetKeyUsage:
    return GetKeyUsage(keys=SqlAlchemyApiKeyRepository(session), quota=quota)


# ── Аутентификация ключа (X-API-Key) ─────────────────────────────────────────


def get_authenticate_api_key(
    session: SessionDep, quota: QuotaDep
) -> AuthenticateApiKey:
    return AuthenticateApiKey(
        keys=SqlAlchemyApiKeyRepository(session),
        generator=SecretsKeyGenerator(),
        quota=quota,
    )


async def require_api_key(
    uc: Annotated[AuthenticateApiKey, Depends(get_authenticate_api_key)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> ApiKey:
    """Гард сигналов: валидный ключ + списание квоты (иначе 401/429)."""
    if not x_api_key:
        raise InvalidApiKeyError("Требуется заголовок X-API-Key")
    return await uc.execute(plaintext=x_api_key)


ApiKeyDep = Annotated[ApiKey, Depends(require_api_key)]


# ── Сигналы ──────────────────────────────────────────────────────────────────


def get_consensus_signal(session: SessionDep) -> GetConsensusSignal:
    return GetConsensusSignal(gateway=SqlAlchemyB2bSignalGateway(session))


def get_leaderboard_signal(session: SessionDep) -> GetLeaderboardSignal:
    return GetLeaderboardSignal(gateway=SqlAlchemyB2bSignalGateway(session))


def get_list_event_signals(session: SessionDep) -> ListEventSignals:
    return ListEventSignals(gateway=SqlAlchemyB2bSignalGateway(session))
