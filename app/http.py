"""Общие HTTP-хелперы, не привязанные к конкретному домену."""

from __future__ import annotations

from starlette.requests import Request


def client_ip(request: Request) -> str | None:
    """IP клиента с учётом обратного прокси.

    Прод стоит за реверс-прокси — ``request.client.host`` там всегда адрес
    прокси, а не клиента, поэтому берём первый адрес из ``X-Forwarded-For``
    (его добавляет прокси; при цепочке прокси — самый левый, ближний к
    клиенту). Без заголовка (прямое подключение, локальная разработка) —
    ``request.client.host``, либо ``None``, если соединение не сетевое.

    Единая точка для всех мест, которым нужен «настоящий» IP клиента —
    rate-limiter (``app/middleware/rate_limit.py``) и юридическая фиксация
    согласий (``identity.application.use_cases.CompleteOnboarding``): раньше
    у каждого была своя копия, что рискованно менять синхронно.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else None
