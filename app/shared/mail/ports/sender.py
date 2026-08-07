"""Порт отправки писем."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.shared.mail.domain.message import EmailMessage


@runtime_checkable
class EmailSender(Protocol):
    """Транспорт писем (SMTP в бою, лог — при ненастроенной почте).

    Реализация вправе бросать исключение при сбое доставки; вызывающий сам
    решает, критично ли это для его сценария. Для входа по ссылке — не
    критично: ``RequestEmailLogin`` ловит сбой и всё равно отвечает 202,
    иначе по коду ответа можно было бы различать существующие адреса.
    """

    async def send(self, message: EmailMessage) -> None:
        """Отправляет письмо; поднимает исключение при сбое доставки."""
        ...
