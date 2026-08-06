"""Общие HTTP-хелперы, не привязанные к конкретному домену."""

from __future__ import annotations

import ipaddress

from starlette.requests import Request


def _valid_ip(candidate: str | None) -> str | None:
    """Нормализует строку в IP-адрес (v4/v6) или отдаёт ``None``.

    Значение уходит в колонку ``inet`` (``user_consents.ip``) и в ключи
    rate-limiter'а, поэтому мусор до них доходить не должен: невалидный
    ``inet`` — это ``DataError`` и 500 на записи согласий, а невалидный ключ —
    возможность развести счётчик лимита произвольным заголовком.
    """
    if not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate.strip()))
    except ValueError:
        return None


def client_ip(request: Request) -> str | None:
    """IP клиента с учётом обратного прокси.

    Прод стоит за реверс-прокси — ``request.client.host`` там всегда адрес
    прокси, а не клиента, поэтому берём первый адрес из ``X-Forwarded-For``
    (его добавляет прокси; при цепочке прокси — самый левый, ближний к
    клиенту). Без заголовка (прямое подключение, локальная разработка) —
    ``request.client.host``, либо ``None``, если соединение не сетевое.

    **Граница доверия.** ``X-Forwarded-For`` — заголовок, который клиент может
    прислать сам: доверять первому адресу можно ТОЛЬКО за прокси, который
    перезаписывает (а не дополняет) заголовок своим значением. Именно так
    настроен ingress демо-контура; при смене схемы фронтирования это место
    придётся пересмотреть. Пока же единственная защита от подделки —
    синтаксическая: любой невалидный токен отбрасывается и подменяется
    адресом сокета (``request.client.host``, тоже проверяется), а если
    валидного адреса нет вовсе — ``None`` (для ``user_consents.ip`` это
    штатный ``NULL``, а не ошибка).

    Единая точка для всех мест, которым нужен «настоящий» IP клиента —
    rate-limiter (``app/middleware/rate_limit.py``) и юридическая фиксация
    согласий (``identity.application.use_cases.CompleteOnboarding``): раньше
    у каждого была своя копия, что рискованно менять синхронно.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        first = _valid_ip(forwarded.split(",")[0])
        if first is not None:
            return first
    return _valid_ip(request.client.host if request.client else None)
