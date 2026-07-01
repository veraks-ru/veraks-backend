"""Эндпоинт авторизации WebSocket-соединений для goctopus.

goctopus (proxy-authorizer) пересылает сюда upgrade-запрос с cookie. Возвращаем
идентификатор пользователя в поле ``email`` (его извлекает ``Export()`` goctopus)
— это ключ, по которому пользователю доставляются пуши. Неаутентифицированный
запрос → пустой ключ → соединение отклоняется.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header

from app.modules.identity.api.dependencies import get_current_user_uc
from app.modules.identity.application.use_cases import GetCurrentUser
from app.modules.identity.domain.errors import IdentityError

router = APIRouter(tags=["notifications"])


def _bearer(header: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return None


@router.get("/realtime/ws-auth", summary="WS-авторизация (для goctopus)")
async def ws_auth(
    uc: Annotated[GetCurrentUser, Depends(get_current_user_uc)],
    authorization: Annotated[str | None, Header()] = None,
    access_token: Annotated[str | None, Cookie()] = None,
) -> dict[str, dict[str, str]]:
    token = _bearer(authorization) or access_token
    if not token:
        return {"user": {"email": "", "organization_name": ""}}
    try:
        user = await uc.from_access_token(token)
    except IdentityError:
        return {"user": {"email": "", "organization_name": ""}}
    # user_id кладём в поле email — его читает Export() goctopus как ключ.
    return {"user": {"email": str(user.id), "organization_name": ""}}
