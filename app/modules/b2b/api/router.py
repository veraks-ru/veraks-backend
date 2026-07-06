"""Роутер B2B: управление ключами (JWT) и signal API (X-API-Key)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.b2b.api.dependencies import (
    AdminUser,
    ApiKeyDep,
    get_consensus_signal,
    get_issue_api_key,
    get_key_usage,
    get_leaderboard_signal,
    get_list_event_signals,
    get_list_my_api_keys,
    get_revoke_api_key,
)
from app.modules.b2b.api.schemas import (
    ApiKeyCreateRequest,
    ApiKeyResponse,
    ConsensusSignalResponse,
    EventSignalResponse,
    IssuedApiKeyResponse,
    LeaderboardSignalRowResponse,
)
from app.modules.b2b.application.use_cases import (
    GetConsensusSignal,
    GetKeyUsage,
    GetLeaderboardSignal,
    IssueApiKey,
    ListEventSignals,
    ListMyApiKeys,
    RevokeApiKey,
)
from app.modules.identity.api.dependencies import CurrentUser

router = APIRouter(tags=["b2b"])


# ── Управление ключами (кабинет владельца, JWT) ──────────────────────────────


@router.post(
    "/b2b/keys",
    response_model=IssuedApiKeyResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Выдать API-ключ (секрет показывается один раз)",
)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    admin: AdminUser,
    uc: Annotated[IssueApiKey, Depends(get_issue_api_key)],
) -> IssuedApiKeyResponse:
    issued = await uc.execute(
        owner_user_id=admin.id,
        name=payload.name,
        daily_quota=payload.daily_quota,
    )
    return IssuedApiKeyResponse.from_issued(issued)


@router.get(
    "/b2b/keys",
    response_model=list[ApiKeyResponse],
    summary="Мои API-ключи",
)
async def list_api_keys(
    current_user: CurrentUser,
    uc: Annotated[ListMyApiKeys, Depends(get_list_my_api_keys)],
) -> list[ApiKeyResponse]:
    keys = await uc.execute(owner_user_id=current_user.id)
    return [ApiKeyResponse.from_domain(k) for k in keys]


@router.get(
    "/b2b/keys/{key_id}/usage",
    response_model=ApiKeyResponse,
    summary="Расход суточной квоты ключа",
)
async def key_usage(
    key_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[GetKeyUsage, Depends(get_key_usage)],
) -> ApiKeyResponse:
    usage = await uc.execute(owner_user_id=current_user.id, key_id=key_id)
    return ApiKeyResponse.from_usage(usage)


@router.delete(
    "/b2b/keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Отозвать ключ",
)
async def revoke_api_key(
    key_id: uuid.UUID,
    current_user: CurrentUser,
    uc: Annotated[RevokeApiKey, Depends(get_revoke_api_key)],
) -> None:
    await uc.execute(owner_user_id=current_user.id, key_id=key_id)


# ── Signal API (внешние потребители, X-API-Key) ──────────────────────────────


@router.get(
    "/v1/signals/consensus/{event_id}",
    response_model=ConsensusSignalResponse,
    summary="Консенсус толпы по событию",
)
async def signal_consensus(
    event_id: uuid.UUID,
    _key: ApiKeyDep,
    uc: Annotated[GetConsensusSignal, Depends(get_consensus_signal)],
) -> ConsensusSignalResponse:
    signal = await uc.execute(event_id=event_id)
    return ConsensusSignalResponse.from_signal(signal)


@router.get(
    "/v1/signals/leaderboard",
    response_model=list[LeaderboardSignalRowResponse],
    summary="Рейтинг предсказателей",
)
async def signal_leaderboard(
    _key: ApiKeyDep,
    uc: Annotated[GetLeaderboardSignal, Depends(get_leaderboard_signal)],
    scope: str = "global",
    scope_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[LeaderboardSignalRowResponse]:
    rows = await uc.execute(scope=scope, scope_id=scope_id, limit=limit)
    return [LeaderboardSignalRowResponse.from_row(r) for r in rows]


@router.get(
    "/v1/signals/events",
    response_model=list[EventSignalResponse],
    summary="Лента событий",
)
async def signal_events(
    _key: ApiKeyDep,
    uc: Annotated[ListEventSignals, Depends(get_list_event_signals)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[EventSignalResponse]:
    signals = await uc.execute(status=status_filter, limit=limit)
    return [EventSignalResponse.from_signal(s) for s in signals]
