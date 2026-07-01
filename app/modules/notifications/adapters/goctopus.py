"""Пуш уведомлений в реальном времени через goctopus (WS-релей).

Бэкенд POST-ит сообщение с ключом = user_id; goctopus доставляет его в
активные WebSocket-соединения этого пользователя. Ошибки пуша проглатываются:
основной запрос не должен падать из-за недоступности релея.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import RealtimeSettings


class GoctopusPusher:
    def __init__(self, settings: RealtimeSettings) -> None:
        self._settings = settings

    async def push(self, key: str, value: dict[str, Any]) -> None:
        if not self._settings.url:
            return
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                await client.post(
                    self._settings.url,
                    json={"key": key, "value": value},
                    auth=(self._settings.user, self._settings.password),
                )
        except Exception:  # noqa: BLE001 — пуш best-effort, не критичен
            pass
