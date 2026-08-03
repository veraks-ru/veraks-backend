"""Порт репозитория согласий пользователей (152-ФЗ).

Бизнес-логика зависит от этого протокола, а не от SQLAlchemy. Реализация —
``adapters/repository.py`` (``SqlAlchemyConsentRepository``); в тестах
подставляется in-memory фейк (``tests/identity/fakes.py``).
"""

from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable

from app.modules.identity.domain.consent import Consent


@runtime_checkable
class ConsentRepository(Protocol):
    """Хранилище согласий (append-only)."""

    async def list_for_user(self, user_id: uuid.UUID) -> list[Consent]:
        """Все согласия пользователя (для профиля и проверки полноты набора)."""
        ...

    async def add_many(self, consents: list[Consent]) -> None:
        """Сохраняет согласия.

        Идемпотентно по ``(user_id, document, version)`` — повторное принятие
        уже сохранённой версии молча пропускается (``ON CONFLICT DO NOTHING``
        на стороне адаптера), поэтому эта операция не бросает ошибок
        уникальности и её можно безопасно вызывать при повторном онбординге.
        """
        ...
