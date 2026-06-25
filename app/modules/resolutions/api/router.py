"""FastAPI-роутер домена resolutions.

Тонкий транспорт: парсит вход, делегирует use-case, маппит домен в схему.
Доменные ошибки → HTTP централизованно в ``app/main.py``. RBAC проверяется в
use-cases (через ``Actor``), поэтому защищённые эндпоинты требуют лишь
аутентификации (``ActorDep`` → 401 без токена). Публичные чтения — без актора.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status

from app.modules.resolutions.api.dependencies import (
    ActorDep,
    get_decide_dispute,
    get_fix_resolution,
    get_list_disputes,
    get_raise_dispute,
    get_resolution,
)
from app.modules.resolutions.api.schemas import (
    DecideDisputeRequest,
    DisputeResponse,
    FixResolutionRequest,
    RaiseDisputeRequest,
    ResolutionResponse,
)
from app.modules.resolutions.application.use_cases import (
    DecideDispute,
    FixResolution,
    GetResolution,
    ListDisputes,
    RaiseDispute,
)

router = APIRouter(tags=["resolutions"])


# ── Разрешение события ──────────────────────────────────────────────────────


@router.get(
    "/events/{event_id}/resolution",
    response_model=ResolutionResponse,
    summary="Текущее разрешение события",
)
async def get_event_resolution(
    event_id: uuid.UUID,
    uc: Annotated[GetResolution, Depends(get_resolution)],
) -> ResolutionResponse:
    """Возвращает текущий (финальный) исход события (публично)."""
    resolution = await uc.execute(event_id=event_id)
    return ResolutionResponse.from_domain(resolution)


@router.post(
    "/events/{event_id}/resolution",
    response_model=ResolutionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Зафиксировать исход события (editor/arbiter)",
)
async def fix_event_resolution(
    event_id: uuid.UUID,
    payload: FixResolutionRequest,
    actor: ActorDep,
    uc: Annotated[FixResolution, Depends(get_fix_resolution)],
) -> ResolutionResponse:
    """Подводит исход закрытого события и открывает окно оспаривания."""
    resolution = await uc.execute(
        event_id=event_id,
        actor=actor,
        outcome=payload.outcome,
        source_reference=payload.source_reference,
        notes=payload.notes,
    )
    return ResolutionResponse.from_domain(resolution)


# ── Споры ───────────────────────────────────────────────────────────────────


@router.get(
    "/events/{event_id}/disputes",
    response_model=list[DisputeResponse],
    summary="Список оспариваний события",
)
async def list_event_disputes(
    event_id: uuid.UUID,
    uc: Annotated[ListDisputes, Depends(get_list_disputes)],
) -> list[DisputeResponse]:
    """Все споры события (публично, прозрачность арбитража)."""
    disputes = await uc.execute(event_id=event_id)
    return [DisputeResponse.from_domain(d) for d in disputes]


@router.post(
    "/events/{event_id}/disputes",
    response_model=DisputeResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Подать оспаривание (участник события)",
)
async def raise_event_dispute(
    event_id: uuid.UUID,
    payload: RaiseDisputeRequest,
    actor: ActorDep,
    uc: Annotated[RaiseDispute, Depends(get_raise_dispute)],
) -> DisputeResponse:
    """Регистрирует оспаривание и переводит событие в ``disputed``."""
    dispute = await uc.execute(
        event_id=event_id,
        actor=actor,
        reason=payload.reason,
        evidence=payload.evidence,
    )
    return DisputeResponse.from_domain(dispute)


@router.post(
    "/disputes/{dispute_id}/decision",
    response_model=DisputeResponse,
    summary="Решение арбитра по спору (accept/reject, может вызвать overturn)",
)
async def decide_dispute(
    dispute_id: uuid.UUID,
    payload: DecideDisputeRequest,
    actor: ActorDep,
    uc: Annotated[DecideDispute, Depends(get_decide_dispute)],
) -> DisputeResponse:
    """Закрывает спор: отклонение или удовлетворение (пересмотр исхода)."""
    dispute = await uc.execute(
        dispute_id=dispute_id,
        actor=actor,
        accept=payload.accept,
        decision_notes=payload.decision_notes,
        new_outcome=payload.new_outcome,
    )
    return DisputeResponse.from_domain(dispute)
