"""FastAPI-роутер домена events (`/events`, `/categories`).

Эндпоинты тонкие: валидируют вход (pydantic), транслируют в DTO, дёргают
use-case и маппят результат. Вся бизнес-логика и проверки прав/переходов —
в прикладном и доменном слоях. Доменные ошибки маппятся в HTTP
централизованно в ``app/main.py``.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.modules.events.api.dependencies import (
    ActorDep,
    get_approve_event,
    get_cancel_event,
    get_close_event,
    get_create_category,
    get_create_event,
    get_get_event,
    get_list_categories,
    get_list_events,
    get_lock_event_predictions,
    get_propose_event,
    get_publish_event,
    get_reject_event,
    get_update_event,
)
from app.modules.events.api.schemas import (
    CategoryResponse,
    CreateCategoryRequest,
    CreateEventRequest,
    EventResponse,
    RejectEventRequest,
    UpdateEventRequest,
)
from app.modules.events.application.use_cases import (
    ApproveEvent,
    CancelEvent,
    CloseEvent,
    CreateCategory,
    CreateEvent,
    GetEvent,
    ListCategories,
    ListEvents,
    ProposeEvent,
    PublishEvent,
    RejectEvent,
    UpdateEvent,
)
from app.modules.predictions.application.use_cases import LockEventPredictions
from app.modules.events.domain.entities import EventStatus
from app.modules.events.ports.repositories import EventFilter

router = APIRouter(tags=["events"])


# ── Категории ─────────────────────────────────────────────────────────────


@router.get("/categories", response_model=list[CategoryResponse], summary="Дерево категорий")
async def list_categories(
    uc: Annotated[ListCategories, Depends(get_list_categories)],
) -> list[CategoryResponse]:
    """Возвращает плоский список категорий (дерево собирается на клиенте/SSR)."""
    categories = await uc.execute()
    return [CategoryResponse.from_domain(c) for c in categories]


@router.post(
    "/categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать категорию (editor/admin)",
)
async def create_category(
    payload: CreateCategoryRequest,
    actor: ActorDep,
    uc: Annotated[CreateCategory, Depends(get_create_category)],
) -> CategoryResponse:
    """Создаёт категорию; права проверяет доменная политика."""
    category = await uc.execute(actor=actor, data=payload.to_input())
    return CategoryResponse.from_domain(category)


# ── События: чтение (публично) ────────────────────────────────────────────


@router.get("/events", response_model=list[EventResponse], summary="Список событий")
async def list_events(
    uc: Annotated[ListEvents, Depends(get_list_events)],
    status_filter: Annotated[EventStatus | None, Query(alias="status")] = None,
    category_id: uuid.UUID | None = None,
    season_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[EventResponse]:
    """Каталог событий с фильтрами по статусу, категории и сезону."""
    criteria = EventFilter(
        status=status_filter,
        category_id=category_id,
        season_id=season_id,
        limit=limit,
        offset=offset,
    )
    events = await uc.execute(criteria=criteria)
    return [EventResponse.from_domain(e) for e in events]


@router.get("/events/{event_id}", response_model=EventResponse, summary="Детали события")
async def get_event(
    event_id: uuid.UUID,
    uc: Annotated[GetEvent, Depends(get_get_event)],
) -> EventResponse:
    """Возвращает событие по id (404, если не найдено)."""
    event = await uc.execute(event_id=event_id)
    return EventResponse.from_domain(event)


# ── События: запись (editor/admin) ────────────────────────────────────────


@router.post(
    "/events",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Создать событие (editor)",
)
async def create_event(
    payload: CreateEventRequest,
    actor: ActorDep,
    uc: Annotated[CreateEvent, Depends(get_create_event)],
) -> EventResponse:
    """Создаёт черновик события (статус ``draft``)."""
    event = await uc.execute(actor=actor, data=payload.to_input())
    return EventResponse.from_domain(event)


@router.patch("/events/{event_id}", response_model=EventResponse, summary="Редактировать событие")
async def update_event(
    event_id: uuid.UUID,
    payload: UpdateEventRequest,
    actor: ActorDep,
    uc: Annotated[UpdateEvent, Depends(get_update_event)],
) -> EventResponse:
    """Частично редактирует событие (до закрытия приёма)."""
    event = await uc.execute(actor=actor, event_id=event_id, patch=payload.to_input())
    return EventResponse.from_domain(event)


@router.post(
    "/events/{event_id}/publish",
    response_model=EventResponse,
    summary="Опубликовать событие (draft → open)",
)
async def publish_event(
    event_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[PublishEvent, Depends(get_publish_event)],
) -> EventResponse:
    """Открывает приём прогнозов по событию."""
    event = await uc.execute(actor=actor, event_id=event_id)
    return EventResponse.from_domain(event)


# ── Пользовательские предложения и их модерация ───────────────────────────


@router.post(
    "/events/propose",
    response_model=EventResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Предложить событие (подписчик, на модерацию)",
)
async def propose_event(
    payload: CreateEventRequest,
    actor: ActorDep,
    uc: Annotated[ProposeEvent, Depends(get_propose_event)],
) -> EventResponse:
    """Пользователь с активной подпиской предлагает событие (статус ``proposed``)."""
    event = await uc.execute(actor=actor, data=payload.to_input())
    return EventResponse.from_domain(event)


@router.post(
    "/events/{event_id}/approve",
    response_model=EventResponse,
    summary="Одобрить предложение (editor/admin): proposed → draft",
)
async def approve_event(
    event_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[ApproveEvent, Depends(get_approve_event)],
) -> EventResponse:
    """Модерация одобряет предложение — оно становится черновиком редакции."""
    event = await uc.execute(actor=actor, event_id=event_id)
    return EventResponse.from_domain(event)


@router.post(
    "/events/{event_id}/reject",
    response_model=EventResponse,
    summary="Отклонить предложение (editor/admin): proposed → cancelled",
)
async def reject_event(
    event_id: uuid.UUID,
    payload: RejectEventRequest,
    actor: ActorDep,
    uc: Annotated[RejectEvent, Depends(get_reject_event)],
) -> EventResponse:
    """Модерация отклоняет предложение; причина уходит автору уведомлением."""
    event = await uc.execute(actor=actor, event_id=event_id, reason=payload.reason)
    return EventResponse.from_domain(event)


@router.post(
    "/events/{event_id}/close",
    response_model=EventResponse,
    summary="Закрыть приём прогнозов (open → closed)",
)
async def close_event(
    event_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[CloseEvent, Depends(get_close_event)],
    lock: Annotated[LockEventPredictions, Depends(get_lock_event_predictions)],
) -> EventResponse:
    """Закрывает приём (editor/system) и сразу замораживает прогнозы события.

    Закрытие и блокировка идут в одной транзакции запроса: после ручного
    закрытия событие можно скорить (его прогнозы становятся ``is_locked``),
    как и при авто-закрытии воркером по ``closes_at``.
    """
    event = await uc.execute(actor=actor, event_id=event_id)
    await lock.execute(event_id=event_id)
    return EventResponse.from_domain(event)


@router.post(
    "/events/{event_id}/cancel",
    response_model=EventResponse,
    summary="Отменить событие (→ cancelled)",
)
async def cancel_event(
    event_id: uuid.UUID,
    actor: ActorDep,
    uc: Annotated[CancelEvent, Depends(get_cancel_event)],
) -> EventResponse:
    """Отменяет событие (из draft/open/closed)."""
    event = await uc.execute(actor=actor, event_id=event_id)
    return EventResponse.from_domain(event)
