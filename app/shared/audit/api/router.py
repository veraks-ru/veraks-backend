"""FastAPI-роутер общей инфраструктуры аудита: чтение и верификация цепочки.

Оба эндпоинта — только для ``admin`` (см. ``require_admin`` в
``dependencies.py``); журнал отдаётся как есть (``payload`` без трансформации).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.shared.audit.api.dependencies import (
    AdminUser,
    get_list_audit_log,
    get_verify_audit_chain,
)
from app.shared.audit.api.schemas import (
    AuditLogEntryResponse,
    AuditLogPageResponse,
    ChainVerificationResponse,
)
from app.shared.audit.application.list_log import ListAuditLog
from app.shared.audit.application.verify_chain import VerifyAuditChain

router = APIRouter(prefix="/admin/audit-log", tags=["audit"])


def _as_utc(dt: datetime | None) -> datetime | None:
    """Наивный datetime из query (клиент не указал зону) — считаем UTC.

    ``occurred_at`` в БД — ``timestamptz``; сравнение с наивным datetime либо
    падает на уровне драйвера, либо (тише и хуже) молча трактуется не так,
    как ожидал клиент. Явная нормализация здесь — на границе API, а не внутри
    use-case: это про интерпретацию внешнего входа, а не бизнес-правило.
    """
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("", response_model=AuditLogPageResponse, summary="Журнал аудита (admin)")
async def list_audit_log(
    _admin: AdminUser,
    uc: Annotated[ListAuditLog, Depends(get_list_audit_log)],
    action: str | None = None,
    actor_id: uuid.UUID | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    before_id: Annotated[int | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditLogPageResponse:
    """Постраничный журнал (новые сначала); ``before_id`` — курсор «показать ещё»."""
    page = await uc.execute(
        action=action,
        actor_id=actor_id,
        occurred_from=_as_utc(occurred_from),
        occurred_to=_as_utc(occurred_to),
        before_id=before_id,
        limit=limit,
    )
    return AuditLogPageResponse(
        items=[AuditLogEntryResponse.from_domain(e) for e in page.items],
        has_more=page.has_more,
    )


@router.post(
    "/verify",
    response_model=ChainVerificationResponse,
    summary="Проверить целостность хеш-цепочки аудита (admin)",
)
async def verify_audit_log(
    _admin: AdminUser,
    uc: Annotated[VerifyAuditChain, Depends(get_verify_audit_chain)],
) -> ChainVerificationResponse:
    """Синхронный прогон верификации (объём демо это позволяет)."""
    result = await uc.execute()
    return ChainVerificationResponse.from_result(result)
